"""Wording that addresses an AI rather than a human reader.

GENERAL is always on. The packs are wording typical of one setting. They are always
applied to hidden text (where any match is telling) and to visible text only when asked
for with --pack, because visible text such as "a strong candidate" is often ordinary.
"""

import re

GENERAL = [
    r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|any|other|your)\b.{0,20}"
    r"\b(instructions?|prompts?|rules|directions|guidelines|context)\b",
    r"\b(you are|you're|act as|pretend to be|from now on,? you)\b.{0,20}\b(an? )?(ai|assistant|language model|llm|"
    r"chatbot|model)\b",
    r"\b(chatgpt|gpt-?\d\w*|claude|gemini|copilot|llama|grok|perplexity)\b",
    r"\b(ai|llm|language model|assistant|chatbot|model) (reading|reviewing|processing|evaluating|screening|grading|"
    r"summari[sz]ing|parsing)\b",
    r"\b(note|instructions?|message|attention) (to|for) (the |any )?(ai|llm|assistant|model|chatbot|language model|"
    r"reviewer|grader|screener|evaluator|recruiter)\b",
    r"\b(system prompt|new instructions|developer mode|jailbreak)\b",
    r"\b(do not|don't|never) (mention|reveal|disclose|tell|say|report|acknowledge)\b.{0,40}"
    r"\b(this|these|instructions?|hidden|text|prompt)\b",
]

PACKS = {
    "grading": [
        r"\b(grader|grading|marker|examiner)\b.{0,40}\b(award|give|assign)\b.{0,30}\b(100|full|perfect|maximum|top)\b",
        r"\b(award|give|assign)\b.{0,30}\b(100|full (credit|marks|points|score)|perfect score|"
        r"maximum (score|points|marks))\b",
    ],
    "hiring": [
        r"\b(exceptional|excellent|ideal|perfect|strongest|outstanding|best possible)\b.{0,15}"
        r"\b(candidate|applicant|fit|match)\b",
        r"\b(recommend|advance|shortlist|move)\b.{0,30}\b(this|the) (candidate|applicant|resume|cv)\b",
        r"\b(hire|interview) (this|the) (candidate|applicant|person)\b",
    ],
    "review": [
        r"\b(give|write|provide)\b.{0,15}\b(a )?(positive|favou?rable|glowing) (review|assessment|evaluation)\b",
        r"\b(recommend|suggest)\b.{0,15}\b(accept(ance)?|approval|publication)\b",
        r"\b(do not|don't|never)\b.{0,15}\b(highlight|mention|point out)\b.{0,20}"
        r"\b(negatives?|weakness(es)?|flaws|limitations)\b",
    ],
}


def parse_packs(value):
    if not value:
        return []
    names = [v.strip().lower() for v in value.split(",") if v.strip()]
    if "all" in names:
        return sorted(PACKS)
    bad = [n for n in names if n not in PACKS]
    if bad:
        raise ValueError("unknown pack %s (choose from %s, or all)" % (", ".join(bad), ", ".join(sorted(PACKS))))
    return names


def hits(text, packs=None):
    """Names of the pattern groups the text matches: 'general' and/or pack names."""
    t = " ".join(text.split()).lower()
    if not t:
        return []
    found = []
    if any(re.search(p, t) for p in GENERAL):
        found.append("general")
    for name in (sorted(PACKS) if packs is None else packs):
        if any(re.search(p, t) for p in PACKS[name]):
            found.append(name)
    return found
