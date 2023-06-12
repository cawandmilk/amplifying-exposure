import torch
import torch.nn.functional as F
import lightning as L

import easydict

from deepspeed.ops.adam import DeepSpeedCPUAdam

from src.score import GPTScorer


class MinimumRiskTrainingModule(L.LightningModule):
    def __init__(self, tok, model, config):
        super(MinimumRiskTrainingModule, self).__init__()

        self.tok = tok
        self.model = model
        self.config = config

        self.score_fn = GPTScorer(tok=tok, model=model)
        self.alpha = 100.0

        ## Force to calculate gradient graph.
        ## See: https://discuss.huggingface.co/t/how-to-output-loss-from-model-generate/16999
        # from undecorated import undecorated
        # from types import MethodType
        # gen_with_grad = undecorated(self.model.generate)
        # self.model.generate_with_grad = MethodType(gen_with_grad, self.model)

        ## Save hyper-parameters to self.hparams (auto-logged by W&B).
        self.save_hyperparameters(ignore=["model"])

    def _get_weighted_loss(
        self,
        loss: torch.Tensor,
        reward: torch.Tensor,
    ) -> torch.Tensor:
        ##  - |loss| = (batch_size,)
        ##  - |reward| = (batch_size,)
        reward = reward.to(device=loss.device, dtype=loss.dtype)
        loss = (loss * F.sigmoid(-reward / self.alpha)).sum()
        # loss = (loss * F.relu6(1 / reward)).sum()
        # loss = (loss * (1 + 1 / torch.sqrt(1 + reward))).sum()

        ## Following two equations are eventually same.
        ## \theta = \theta - risk * \nabla_\theta \log{P}
        ## \theta = \theta - -reward * \nabla_\theta \log{P}
        ## where risk = -reward.

        return loss

    def _get_reward(self, gen_tokens: torch.Tensor) -> dict:
        ## Calculate membership inference metrics for all samples.
        l = self.score_fn.ce_loss(gen_tokens)
        p = torch.exp(l)
        z = self.score_fn.zlib_entropy(gen_tokens).to(l.device)
        ## |l| = (batch_size,)
        ## |p| = (batch_size,)
        ## |z| = (batch_size,)

        s = z / p  ## score
        ## |s| = (batch_size,)

        return easydict.EasyDict({"ce": l, "ppl": p, "zlib": z, "score": s})

    def forward(self, *args, **kwargs):
        return self.model(*args, **kwargs)

    def configure_optimizers(self):
        return DeepSpeedCPUAdam(self.parameters(), lr=self.config.lr)

    def training_step(self, batch: dict, batch_idx) -> torch.Tensor:
        ## Maximum likelihood.
        ##  - |x| = (batch_size, 1) -> only <EOS>
        x = batch["input_ids"]

        prompt_len = x.size(1)
        assert prompt_len == 1

        ## Sampling y_hat.
        ##  - |gen_tokens| = (batch_size, length)
        with torch.no_grad():
            gen_tokens = self.model.generate(
                x,
                do_sample=True,
                temperature=self.config.temperature,  ## 1.0
                repetition_penalty=self.config.repetition_penalty,
                min_new_tokens=self.config.min_length - prompt_len,  ## 63
                max_new_tokens=self.config.max_length - prompt_len,  ## 63
                top_p=self.config.top_p,  ## 0.95
                top_k=self.config.top_k,  ## 40
            )

        ## Calcuate score and somethings.
        r_dict = self._get_reward(gen_tokens)

        with torch.no_grad():
            ## Based on the result of sampling, get reward.
            ## We set the generated sentence with the highest score
            ## in the batch as the target.
            ##  - |actor_reward| = (batch_size,)
            actor_reward = r_dict.score

            ## Take samples as many as n_samples, and get average rewards for them.
            ## I figured out that n_samples = 1 would be enough.
            baseline = []

            for _ in range(self.config.rl_n_samples):
                sampled_gen_tokens = self.model.generate(
                    x,
                    do_sample=True,
                    temperature=self.config.temperature,  ## 1.0
                    repetition_penalty=self.config.repetition_penalty,
                    min_new_tokens=self.config.min_length - prompt_len,  ## 63
                    max_new_tokens=self.config.max_length - prompt_len,  ## 63
                    top_p=self.config.top_p,  ## 0.95
                    top_k=self.config.top_k,  ## 40
                )
                baseline += [self._get_reward(sampled_gen_tokens).score]

            ## Get average of k samples.
            ##  - |baseline| = (rl_n_samples, batch_size) -> (batch_size,)
            baseline = torch.stack(baseline).mean(dim=0)

            ## Now, we have relatively expected cumulative reward.
            ## Which score can be drawn from actor_reward substracted by baseline.
            ##  - |reward| = (batch_size,) \in (-inf, +inf)
            reward = actor_reward - baseline

        ## Calculate gradients with back-propagation.
        loss = self._get_weighted_loss(r_dict.ce, reward=reward)

        ## Make a return dict.
        metrics = {"loss": loss, "ppl": r_dict.ppl, "zlib": r_dict.zlib}
        self.log_dict(metrics, prog_bar=True, logger=True, on_step=True)
        # self.log("loss", loss, prog_bar=True, logger=True, on_step=True)

        return metrics
        # return loss
