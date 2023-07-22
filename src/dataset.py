from operator import itemgetter
from typing import List


def make_pairs(texts: List[str], scores: List[float]) -> List[dict]:
    """Pair text for RLHF training.

    Args:
        texts (List[str]): Texts generated by target LM
        scores (List[float]): Score of texts generated by target LM

    Returns:
        List[dict]: A list of dictionaries containing prompt, chosen, and rejected
    """
    assert len(scores) == len(texts)

    ## Sort by ascending.
    items = [{"score": s, "text": t} for s, t in zip(scores, texts)]
    items = sorted(items, key=itemgetter("score"))

    ## Make even.
    if len(items) % 2:
        items = items[1:]

    ## Pair locally optimal so that the score difference is maximized.
    low = items[: len(items) // 2]
    high = items[len(items) // 2 :]

    ## Determines chosen and rejected.
    pairs = []
    for l, h in zip(low, high):
        ## A larger log probability is fake text
        ##  == Smaller negative log probability is fake text
        chosen = l["text"]
        rejected = h["text"]

        items = {
            "prompt": "",  ## empty prompt
            "chosen": chosen,
            "chosen_score": float(l["score"]),
            "rejected": rejected,
            "rejected_score": float(h["score"]),
            "score_diff": float(h["score"] - l["score"]),
        }
        # items = {
        #     "prompt": "Human: " + "" + " Assistant:",  ## empty prompt
        #     "chosen": chosen,
        #     "rejected": rejected,
        #     "score_diff": score_diff,
        # }
        pairs.append(items)

    return pairs
