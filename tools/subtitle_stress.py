"""Stress test for the subtitle word decisions (subtitles._evidence and friends).

Synthetic transcripts and captions, no media: every built-in swear against the
ways captions soften it, the dictionary's sound-alikes of every swear, random
damage to the script around a word, from_subtitles placement, and subtitles
knocked out of line. The one outcome that must never happen is a real swear
left in - a softening that comes back "soundalike".

    python tools/subtitle_stress.py [out.md]

Writes a markdown table of every case and prints a summary.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from censarr import sounds, subtitles, words  # noqa: E402

M = words.Matcher(context_words=frozenset())
ROWS: list[tuple[str, str, str, str, str]] = []     # category, heard, caption, expected, got


def said(text: str, start: float = 10.0, gap: float = 0.35) -> list[dict]:
    return [{"word": " " + w, "start": round(start + n * gap, 3),
             "end": round(start + n * gap + 0.3, 3)} for n, w in enumerate(text.split())]


def detections(heard: list[dict]) -> list[words.Match]:
    return words.Matcher(context_words=frozenset()).find(heard)


def verdict(heard, cues, target: str | None = None) -> list[tuple[str, str]]:
    """(word, state) for each detection (or only `target`)."""
    out = []
    for m in detections(heard):
        if target and words.normalize(m.text) != target:
            continue
        out.append((m.text, subtitles._evidence(m, cues, M, heard)[0] or "none"))
    return out


# ------------------------------------------------------------------ 1. softenings

GENERIC = ["whoa", "wow", "man", "oh no", "come on", "hey", "jeez", "geez", "dang", "darn",
           "heck", "gosh", "crap", "shoot", "jerk", "idiot", "stupid", "freaking", "flipping",
           "frigging", "effing", "fudge", "sugar", "shucks", "blast", "rats", "nuts", "damn",
           "hell", "[bleep]", "****", "-", "stuff", "thing", "that"]
SPECIFIC = {
    "fuck": ["fork", "fudge", "frick", "freak", "flip", "fug", "feck", "frak", "eff", "funk",
             "fun", "fox", "flock", "fire", "forget", "f-", "f--", "f...", "f***", "fu--",
             "F-word", "fook", "fack", "fock", "fuk", "phuck", "frig", "fiddle", "ferk"],
    "fucking": ["forking", "fudging", "fricking", "freaking", "flipping", "fugging",
                "fecking", "frakking", "effing", "f-ing", "f***ing", "friggin'", "fricken",
                "fecken", "flippin'", "funking", "fracking", "flocking", "folking",
                "freakin'", "flaming", "blooming", "bloody", "stinking", "lousy", "darned"],
    "fucked": ["forked", "fudged", "freaked", "flipped", "screwed", "effed", "fried",
               "frigged", "wrecked", "hosed", "toast", "finished", "f***ed"],
    "fucker": ["fudger", "forker", "freaker", "sucker", "trucker", "jerk", "f***er", "flicker"],
    "motherfucker": ["mother", "mother-", "mother--", "mother...", "mother lover",
                     "mothertrucker", "motherfudger", "mother-trucker", "monkey fighter",
                     "melon farmer", "mofo", "mother hubbard", "mother father", "falcon",
                     "mother f***er", "mother-effer", "mother flipper", "muddah", "brother"],
    "motherfucking": ["monkey-fighting", "mother-loving", "mother-flipping", "mothering",
                      "mother-trucking", "Monday-to-Friday", "mother fricking"],
    "shit": ["shoot", "sugar", "shirt", "ship", "sheet", "shut", "shh", "shiz", "shiitake",
             "snot", "spit", "poop", "crud", "stuff", "junk", "sheesh", "s***", "sh*t",
             "sh-", "s-", "shiz", "shite", "shyte", "shizzle", "schnitzel", "shucks", "sit",
             "shift", "chit", "shish", "shush"],
    "shitty": ["shoddy", "crappy", "cruddy", "shabby", "shady", "lousy", "sh*tty", "shirty",
               "shitsy", "chintzy", "sketchy"],
    "bullshit": ["bull", "baloney", "bull-crap", "bullpucky", "BS", "bull-shoot", "bologna",
                 "hogwash", "nonsense", "bull-hockey", "bull-", "bull****", "bullshirt",
                 "bullship", "bullsheet", "bullspit", "rubbish", "garbage"],
    "horseshit": ["horse-crap", "hogwash", "horse feathers", "horse hockey", "horse manure",
                  "horse-", "horse"],
    "shithead": ["dumbhead", "butthead", "jerk", "shit-", "sh*thead", "shiphead", "sheethead",
                 "knucklehead", "meathead", "shed head"],
    "ass": ["butt", "bum", "behind", "rear", "tush", "keister", "donkey", "arse", "as",
            "tail", "hiney", "caboose", "a**", "backside", "rump", "a-", "ask", "ash"],
    "asshole": ["jerk", "a-hole", "a**hole", "butthole", "jerkoff", "arsehole", "idiot",
                "hole", "a-hole", "ash-hole", "ashhole", "a-", "rear end", "donkey", "a*****e"],
    "jackass": ["jerk", "jack", "jackal", "donkey", "jack-", "jackpot"],
    "dumbass": ["dummy", "dumb", "dumbo", "dumbbell", "doofus", "dumb-", "dumb butt"],
    "smartass": ["smart aleck", "wise guy", "smarty", "smart mouth", "smart-"],
    "badass": ["bad", "tough", "cool", "badass", "bad butt", "bad-"],
    "bitch": ["witch", "brat", "beach", "bench", "biscuit", "rich", "pitch", "jerk", "b-",
              "b***h", "B-word", "bee", "bit", "bish", "biatch", "bizzle", "vixen", "wench",
              "gun", "gum"],
    "bitches": ["witches", "benches", "beaches", "biscuits", "britches", "riches", "b-es"],
    "bitching": ["griping", "moaning", "whining", "complaining", "witching", "pitching"],
    "bastard": ["buster", "jerk", "dastard", "basket", "baboon", "rascal", "creep", "b-",
                "bass turd", "bustard", "b*****d"],
    "damn": ["darn", "dang", "doggone", "dam", "dan", "damp", "dame", "dent", "d-", "d***",
             "drat", "dash", "dagnabbit", "gosh", "god", "d-a-m-n"],
    "dammit": ["darn it", "dang it", "dagnabbit", "doggone it", "drat it", "shoot", "darnit",
               "damn it", "dam it", "dang"],
    "goddamn": ["gosh darn", "god dang", "goldarn", "doggone", "god", "gol-dang", "gosh-darn",
                "god bless", "gosh dang", "cotton-picking", "g-d", "godd**n"],
    "goddamnit": ["gosh darn it", "god dang it", "doggone it", "god", "gosh darnit"],
    "hell": ["heck", "help", "hail", "hello", "hades", "h-e-double-hockey-sticks", "halifax",
             "h-", "h***", "heaven", "earth", "blazes", "world", "hey", "hull", "hall", "well",
             "held", "helm"],
    "piss": ["pee", "peed", "tinkle", "pass", "kiss", "p-", "p***", "piece", "pierce", "fizz"],
    "pissed": ["ticked", "peed", "miffed", "peeved", "teed", "annoyed", "mad", "furious",
               "p***ed", "pissy", "passed", "pist", "pieced", "priced", "past", "pressed"],
    "crap": ["crud", "crab", "carp", "cramp", "junk", "poop", "crepe", "clap", "trap",
             "stuff", "garbage", "cr*p"],
    "dick": ["jerk", "dork", "dink", "dip", "dig", "duck", "rick", "tick", "dickens", "dix",
             "doc", "d**k", "d-", "deck", "dirk"],
    "dickhead": ["dork", "butthead", "dipstick", "doofus", "deadhead", "dickhead", "dick-"],
    "cock": ["rooster", "cork", "caulk", "clock", "cop", "cot", "c**k", "cock-", "coke",
             "crock", "hawk"],
    "pussy": ["wimp", "wuss", "puss", "posse", "pushy", "kitty", "sissy", "p***y", "pussycat",
              "chicken", "baby", "coward"],
    "prick": ["jerk", "brick", "trick", "prig", "pick", "prince", "p***k", "creep"],
    "cunt": ["count", "can't", "cut", "c-word", "runt", "punt", "c***", "c-", "witch",
             "bitch", "cow", "cant"],
    "twat": ["twit", "twot", "twerp", "twad", "twa", "tw*t", "prat", "idiot"],
    "wanker": ["wally", "banker", "tanker", "plonker", "anchor", "w*nker", "wonker", "idiot",
               "tosser", "prat"],
    "bollocks": ["rubbish", "nonsense", "bullocks", "bollards", "blocks", "b*llocks", "balls",
                 "bollix", "codswallop"],
    "bugger": ["beggar", "blighter", "bother", "booger", "b*gger", "bugga", "bugged"],
    "douche": ["dude", "jerk", "dweeb", "douse", "doosh", "d-bag"],
    "douchebag": ["dirtbag", "scumbag", "jerk", "douche-", "dweeb", "d-bag", "sleazebag"],
    "tits": ["bits", "boobies", "chest", "tots", "teats", "tips", "t*ts", "tats"],
    "boobs": ["boobies", "chest", "boos", "boots", "b**bs", "breasts"],
    "whore": ["tramp", "hoe", "hoar", "horse", "floozy", "hooker", "wh*re", "hour", "war",
              "four", "or", "ho"],
    "slut": ["tramp", "slob", "floozy", "sl*t", "slug", "slot", "slit", "smut", "slat",
             "sloth"],
    "skank": ["skunk", "scank", "tramp", "sk*nk", "skate", "stank", "shank"],
    "retard": ["regard", "moron", "dummy", "r-word", "r*tard", "reward", "retort"],
    "retarded": ["restarted", "stupid", "dumb", "regarded", "rewarded", "r-word"],
    "fag": ["fog", "flag", "fig", "f-word", "f*g", "fad", "fang", "jerk"],
    "faggot": ["maggot", "fagot", "bassoon", "f*ggot", "f-word", "faggit", "fidget"],
    "nigger": ["[slur]", "n-word", "n*****", "brother", "man", "trigger", "nigga",
               "niger", "nicker", "bigger", "digger", "knickers"],
    "nigga": ["[slur]", "n-word", "n****", "brother", "man", "dude", "homie", "nigger",
              "niger", "nicka", "bigga", "neither"],
    "spic": ["spec", "speck", "spike", "[slur]", "sp*c", "spick", "spin"],
    "kike": ["kite", "kick", "[slur]", "k*ke", "keg", "kick"],
    "gook": ["goo", "gook", "cook", "[slur]", "g**k", "gourd"],
    "jizz": ["jazz", "juice", "j*zz", "gist", "chizz", "jeez"],
    "cum": ["come", "calm", "cam", "c*m", "gum", "comb"],
    "horny": ["hungry", "honey", "corny", "frisky", "thorny", "hornet", "randy", "h*rny"],
    "boner": ["bone", "bonner", "boater", "bummer", "b*ner", "stiffy"],
    "hooker": ["hooter", "looker", "hoofer", "hookah", "hook"],
    "arse": ["bum", "behind", "ass", "as", "arts", "a**e", "arch"],
    "wank": ["wonk", "wink", "bank", "tank", "w*nk", "whack"],
    "blowjob": ["blowdryer", "blow job", "b*owjob", "job", "blow", "blowout"],
    "jesus": ["jeez", "geez", "jeepers", "gee", "jiminy", "jesse", "cheeses", "jesus christ",
              "gosh", "gee whiz", "cheese", "j*sus", "jeezy", "zeus"],
    "christ": ["cripes", "crikey", "crisp", "chris", "crust", "cheese", "christmas", "chr*st",
               "crust", "crisis", "crime", "christ's"],
}

TEMPLATES = [
    ("start", "{} I forgot the keys again", "{}, I forgot the keys again."),
    ("middle", "I told you the {} window was never going to work",
     "I told you the {} window was never going to work."),
    ("end", "we are really in trouble now {}", "We are really in trouble now, {}!"),
]
PHRASES = {   # swear: (heard with the swear, caption with {} for the substitute)
    "bitch": ("you son of a bitch get back here", "You son of a {}, get back here!"),
    "fuck": ("what the fuck is going on in here", "What the {} is going on in here?"),
    "motherfucker": ("yippee ki yay motherfucker", "Yippee-ki-yay, {}."),
    "shit": ("oh shit oh shit they found us", "Oh, {}, oh, {}, they found us!"),
    "hell": ("get the hell out of my house", "Get the {} out of my house!"),
    "ass": ("move your ass we are late", "Move your {}, we are late!"),
    "damn": ("damn it all to hell", "{} it all to heck"),
    "christ": ("oh christ not again", "Oh, {}, not again."),
    "jesus": ("jesus christ look at that", "{}, look at that!"),
}


def softening_cases(only: str | None = None) -> list[dict]:
    fails = []
    for swear in sorted(set(SPECIFIC) if only is None else {only}):
        subs = list(dict.fromkeys(SPECIFIC.get(swear, []) + GENERIC))
        for sub in subs:
            caption_word = "" if sub == "-" else sub
            forms = []
            for where, heard_t, cap_t in TEMPLATES:
                forms.append((where, heard_t.format(swear), cap_t.format(caption_word)))
            if swear in PHRASES:
                h, c = PHRASES[swear]
                forms.append(("phrase", h, c.replace("{}", caption_word)))
            for where, heard_text, caption in forms:
                heard = said(heard_text)
                caption = " ".join(caption.split())
                cues = [subtitles.Cue(heard[0]["start"] - 0.2, heard[-1]["end"] + 0.4, caption)]
                got = verdict(heard, cues)
                states = [s for w, s in got if words.normalize(w) == swear] or \
                         [s for _w, s in got]
                state = states[0] if states else "not detected"
                ROWS.append(("1 softening, " + where, heard_text, caption, "not soundalike", state))
                if state == "soundalike":
                    fails.append({"swear": swear, "sub": sub, "where": where,
                                  "heard": heard_text, "caption": caption})
            # No anchors: nothing heard around it appears in the caption.
            heard = said(f"alpha bravo {swear} charlie delta")
            m = heard[2]
            if not caption_word:
                continue
            for cap in (f"{caption_word}!", f"Oh, {caption_word}, come on!"):
                cues = [subtitles.Cue(m["start"] - 0.3, m["end"] + 0.3, cap)]
                got = verdict(heard, cues, swear)
                state = got[0][1] if got else "not detected"
                ROWS.append(("1 softening, no anchor", f"alpha bravo {swear} charlie delta",
                             cap, "not soundalike", state))
                if state == "soundalike":
                    fails.append({"swear": swear, "sub": sub, "where": "no anchor",
                                  "heard": f"… {swear} …", "caption": cap})
    return fails




# ------------------------------------------------------------------ 2. mishearings

def swears() -> list[str]:
    out = set(words.BLASPHEMY_SOLO)
    for group in words.WORDLISTS.values():
        out |= {w for w in group if " " not in w}
    return sorted(out)


def _alike_words(swear: str) -> list[str]:
    out = []
    for w in sounds._dictionary():
        if w == swear or not w.isalpha() or M.find([{"word": w, "start": 0, "end": 0}]):
            continue
        if sounds.sounds_alike(swear, w):
            out.append(w)
    return out


def mishearing_cases(workers: int = 4) -> dict:
    from multiprocessing import Pool
    try:
        from wordfreq import zipf_frequency
    except ImportError:                                # only for ranking
        def zipf_frequency(_w, _l):
            return 0.0
    names = swears()
    with Pool(workers) as pool:
        found = dict(zip(names, pool.map(_alike_words, names)))
    report = {}
    for swear, alike in found.items():
        ranked = sorted(alike, key=lambda w: -zipf_frequency(w, "en"))
        wrong = []
        for w in ranked:
            if zipf_frequency(w, "en") < 3.0:           # rare words: counted, not run
                continue
            heard = said(f"I told you the {swear} window was never going to work")
            cues = [subtitles.Cue(9.8, 14.0, f"I told you the {w} window was never going to work.")]
            got = verdict(heard, cues, swear)
            state = got[0][1] if got else "not detected"
            ROWS.append(("2 mishearing", f"… the {swear} window …", f"… the {w} window …",
                         "soundalike", state))
            if state != "soundalike":
                wrong.append((w, state))
        report[swear] = {"alike": len(alike),
                         "common": [(w, round(zipf_frequency(w, "en"), 1)) for w in ranked
                                    if zipf_frequency(w, "en") >= 4.0],
                         "not_soundalike": wrong}
    return report


# ------------------------------------------------------------------ 3a. reworded captions
# What subtitlers actually do to a line with a swear in it: cut it, reword the
# line around it, contract, or swap in a milder phrase. Every one of these must
# be muted - none may come back soundalike.
REWORDED = [
    ("oh shit she is here", "Oh, she's here."),
    ("oh shit he is back", "Oh, he's back."),
    ("what the hell happened here", "What happened here?"),
    ("shut the fuck up", "Shut up!"),
    ("i do not give a shit", "I don't care."),
    ("fuck it let us go", "Forget it, let's go."),
    ("holy shit look at that", "Holy cow, look at that."),
    ("this is bullshit", "This is nonsense."),
    ("damn it i am late", "Darn it, I'm late."),
    ("damn it i am done", "I'm done."),
    ("fuck you man", "Screw you, man."),
    ("son of a bitch", "Son of a gun."),
    ("get the fuck out", "Get out!"),
    ("hell yeah we did", "Oh, yeah, we did."),
    ("who the hell are you", "Who are you?"),
    ("where the fuck is he", "Where is he?"),
    ("it is fucking cold out here", "It's freezing out here."),
    ("you are fucking kidding me", "You're kidding me."),
    ("i am so fucking tired", "I'm so tired."),
    ("fucking hell mate", "Bloody hell, mate."),
    ("jesus christ what now", "Geez, what now?"),
    ("goddamn it not again", "Doggone it, not again."),
    ("piss off you idiot", "Push off, you idiot."),
    ("piss off you idiot", "Buzz off, you idiot."),
    ("screw that shit", "Screw that stuff."),
    ("kiss my ass", "Kiss my butt."),
    ("what an asshole", "What a jerk."),
    ("do not be a dick", "Don't be a jerk."),
    ("stop being such a pussy", "Stop being such a baby."),
    ("shit happens", "Stuff happens."),
    ("no shit sherlock", "No kidding, Sherlock."),
    ("hell no", "Heck no."),
    ("oh fuck", "Oh, no."),
    ("oh fuck", "Oh, God."),
    ("oh fuck", "Oh, man."),
    ("fucking idiot", "Stupid idiot."),
    ("what the fuck did you do", "What did you do?"),
    ("i said get your ass in here", "I said get in here."),
    ("he is a piece of shit", "He's a piece of work."),
    ("that is some good shit", "That is some good stuff."),
    ("are you fucking serious", "Are you serious?"),
    ("i am pissed at him", "I'm mad at him."),
    ("oh my god oh my god", "Oh, my gosh, oh, my gosh."),
    ("shit shit shit", "No, no, no!"),
    ("fuck fuck fuck", "Oh, no, no, no!"),
    ("the cunt took it", "The creep took it."),
    ("damn you are good", "Dang, you're good."),
    ("well shit that is that", "Well, that's that."),
    ("holy fucking shit", "Holy cow."),
    ("shit i had it here", "I had it here."),
    ("shit she had it here", "She had it here."),
    ("hell he has it", "He has it."),
    ("damn it all i am done here", "I'm done here."),
    ("tits out lads", "It's out, lads."),
    ("we are so fucked now", "We're so done now."),
    ("that is fucked up", "That is messed up."),
    ("fuck off", "Back off."),
    ("bitch please", "Girl, please."),
    ("you little shit", "You little brat."),
    ("dumb bitch", "Dumb witch."),
]


def reworded_cases() -> list[dict]:
    fails = []
    for heard_text, caption in REWORDED:
        heard = said(heard_text)
        cues = [subtitles.Cue(heard[0]["start"] - 0.2, heard[-1]["end"] + 0.4, caption)]
        for word, state in verdict(heard, cues):
            ROWS.append(("3 reworded caption", heard_text, caption, "not soundalike", state))
            if state == "soundalike":
                fails.append({"heard": heard_text, "caption": caption, "word": word})
    return fails


# ------------------------------------------------------------------ 3b. script-reading fuzz

FILLER = ("we need to finish this before the others get back from the store and then "
          "maybe we can talk about what happened last night with your brother at the "
          "party because honestly nobody expected him to show up like that").split()
FUZZ_SWEARS = ["fuck", "shit", "damn", "hell", "bitch", "ass", "fucking", "bullshit",
               "asshole", "dick", "cock", "piss", "crap", "bastard", "pussy", "christ"]
SOFT = ["whoa", "man", "hey", "come on", "no", "wow", "jerk", "freaking", "crap", "heck"]
LABELS = ["RICHIE:", "SYDNEY:", "MAN:", "WOMAN 2:", "- ", "(laughs)", "[sighs]", "<i>", ""]


def _alike_options(swear: str, rng: random.Random) -> list[str]:
    opts = [w for w in ("caulk", "sheet", "dig", "beach", "hail", "slob", "fork", "posse",
                        "pass", "crab", "crisp", "as", "dam")
            if sounds.sounds_alike(swear, w)]
    return opts


def _fuzz_one(rng: random.Random):
    swear = rng.choice(FUZZ_SWEARS)
    before = rng.sample(FILLER, rng.randint(0, 6))
    after = rng.sample(FILLER, rng.randint(0, 6))
    if rng.random() < 0.15:                        # a chant
        heard_words = [swear] * rng.randint(2, 4)
        target = 0
    else:
        heard_words = before + [swear] + after
        target = len(before)
    mode = rng.choice(["keep", "keep", "hide", "drop", "soft", "alike", "other"])
    cap = list(heard_words)
    idx = [i for i, w in enumerate(cap) if w == swear]
    for i in idx:
        if mode == "hide":
            cap[i] = swear[0] + "*" * (len(swear) - 1)
        elif mode == "drop":
            cap[i] = ""
        elif mode == "soft":
            cap[i] = rng.choice(SOFT)
        elif mode == "alike":
            opts = _alike_options(swear, rng)
            cap[i] = rng.choice(opts) if opts else swear
        elif mode == "other":
            cap[i] = rng.choice(["table", "yesterday", "running", "quietly"])
    # Damage to the rest of the caption.
    for _ in range(rng.randint(0, 3)):
        op = rng.choice(["drop", "insert", "swap", "condense"])
        others = [i for i, w in enumerate(cap) if w and w != swear and i not in idx]
        if op == "drop" and others:
            cap[rng.choice(others)] = ""
        elif op == "insert":
            cap.insert(rng.randint(0, len(cap)), rng.choice(FILLER))
        elif op == "swap" and len(others) >= 2:
            a, b = rng.sample(others, 2)
            cap[a], cap[b] = cap[b], cap[a]
        elif op == "condense":
            for i in others:
                if rng.random() < 0.35:
                    cap[i] = ""
    cap = [w for w in cap if w]
    heard = said(" ".join(heard_words), start=20.0, gap=rng.choice([0.25, 0.35, 0.5]))
    if not cap:
        return None
    # Lines: one, or split in two; labels, dashes, italics, asides.
    text_words = [w.capitalize() if n == 0 else w for n, w in enumerate(cap)]
    if rng.random() < 0.4:
        text_words = [w.upper() if rng.random() < 0.1 else w for w in text_words]
    label = rng.choice(LABELS)
    t0, t1 = heard[0]["start"] - rng.uniform(0, 0.4), heard[-1]["end"] + rng.uniform(0, 0.8)
    if rng.random() < 0.3 and len(text_words) >= 4:
        cut = rng.randint(1, len(text_words) - 1)
        mid = t0 + (t1 - t0) * cut / len(text_words)
        cues = [subtitles.Cue(t0, mid, f"{label} {' '.join(text_words[:cut])}".strip()),
                subtitles.Cue(mid + 0.05, t1, f"- {' '.join(text_words[cut:])}!")]
    else:
        cues = [subtitles.Cue(t0, t1, f"{label} {' '.join(text_words)}.".strip())]
    return swear, target, mode, heard, cues


def _caption_tokens(cues):
    return [r for c in cues for r in subtitles._ASIDE.sub(" ", c.text).split()
            if words.normalize(r)]


def fuzz_script(n: int = 20000, seed: int = 7) -> dict:
    rng = random.Random(seed)
    out = {"cases": 0, "crashes": [], "I1": [], "I2": [], "left_in_real_swear": [],
           "alike_missed": 0, "alike_cases": 0, "states": {}}
    for _ in range(n):
        made = _fuzz_one(rng)
        if made is None:
            continue
        swear, target, mode, heard, cues = made
        out["cases"] += 1
        try:
            found = [m for m in detections(heard) if words.normalize(m.text) == swear]
            got = [subtitles._evidence(m, cues, M, heard) for m in found]
        except Exception as exc:                   # noqa: BLE001
            out["crashes"].append((repr(exc), swear, [h["word"] for h in heard],
                                   [c.text for c in cues]))
            continue
        tokens = _caption_tokens(cues)
        heard_plain = {subtitles._plain(words.normalize(h["word"])) for h in heard}
        for m, (state, _line, instead) in zip(found, got):
            out["states"][(mode, state)] = out["states"].get((mode, state), 0) + 1
            case = (swear, mode, " ".join(h["word"].strip() for h in heard),
                    " / ".join(c.text for c in cues), state, instead)
            if state == "soundalike":
                # I1: something in the caption sounds like it, and was not heard.
                ok = any(sounds.sounds_alike(swear, t) and
                         subtitles._plain(words.normalize(t)) not in heard_plain for t in tokens)
                if not ok and not (instead and sounds.sounds_alike(swear, instead)):
                    out["I1"].append(case)
                # I2: never while the line has the word itself.
                if any(subtitles._is_word(t, swear, M) for t in tokens):
                    out["I2"].append(case)
                if mode in ("keep", "hide", "drop", "soft", "other"):
                    out["left_in_real_swear"].append(case)
            if mode == "alike":
                out["alike_cases"] += 1
                if state != "soundalike":
                    out["alike_missed"] += 1
    return out


# ------------------------------------------------------------------ 4. from_subtitles fuzz

NOT_SWEARS = ["95%", "$5,000", "100%", "£50", "24/7", "#1", "50/50", "$20", "3.5%", "€10",
              "Ke$ha", "A$AP", "M*A*S*H", "C#", "F#", "joe@home.com", "E*Trade", "Q*bert",
              "@mentions", "*sigh*", "*NSYNC", "#hashtag", "P!nk", "B*Witched", "Yahoo!"]
MISHEARD = {"damn": "dan", "pussy": "posse", "shit": "ship", "fuck": "fork", "hell": "hail",
            "bitch": "beach", "dick": "dig", "ass": "as", "crap": "crab"}


def fuzz_from_subtitles(n: int = 8000, seed: int = 11) -> dict:
    rng = random.Random(seed)
    out = {"cases": 0, "placed": 0, "crashes": [], "overlap": [], "length": [],
           "not_a_swear": [], "duplicate": [], "missed_misheard": [], "on_misheard": 0}
    for _ in range(n):
        swear = rng.choice(list(MISHEARD) + ["fucking", "bullshit", "asshole", "bastard"])
        before = rng.sample(FILLER, rng.randint(2, 6))
        after = rng.sample(FILLER, rng.randint(2, 6))
        kind = rng.choice(["missed", "misheard", "caught", "chant", "junk"])
        gap = 0.35
        if kind == "chant":
            heard_words = before + [swear] * 3 + after
            cap_words = before + [swear] * 3 + after
        elif kind == "junk":
            junk = rng.choice(NOT_SWEARS)
            heard_words = before + ["something"] + after
            cap_words = before + [junk] + after
        else:
            heard_words = list(before) + ([] if kind == "missed" else
                                          [MISHEARD.get(swear, swear) if kind == "misheard"
                                           else swear]) + after
            cap_words = before + [swear] + after
        heard = said(" ".join(heard_words), start=30.0, gap=gap)
        if kind == "missed":                     # room where the swear was said
            for h in heard[len(before):]:
                h["start"] += 0.5
                h["end"] += 0.5
        cues = [subtitles.Cue(heard[0]["start"] - 0.2, heard[-1]["end"] + 0.4,
                              " ".join(cap_words).capitalize() + ".")]
        matches = words.Matcher().find(heard)
        out["cases"] += 1
        try:
            placed = subtitles.from_subtitles(cues, heard, matches, words.Matcher())
        except Exception as exc:                  # noqa: BLE001
            out["crashes"].append((repr(exc), heard_words, cues[0].text))
            continue
        out["placed"] += len(placed)
        case = (kind, " ".join(heard_words), cues[0].text,
                [(p.text, round(p.start, 2), round(p.end, 2)) for p in placed])
        for p in placed:
            if any(p.start < m.end + 0.3 and p.end > m.start - 0.3 for m in matches):
                out["overlap"].append(case)
            if not subtitles.PLACE_MIN <= p.end - p.start <= subtitles.PLACE_MAX:
                out["length"].append(case)
            if kind == "junk":
                out["not_a_swear"].append(case)
        if kind in ("caught", "chant") and placed:
            out["duplicate"].append(case)
        if kind == "misheard":
            w = heard[len(before)]
            on = [p for p in placed if abs(p.start - w["start"]) < 0.01 and abs(p.end - w["end"]) < 0.01]
            covered = any(m.start <= w["start"] + 0.05 and m.end >= w["end"] - 0.05 for m in matches)
            if on:
                out["on_misheard"] += 1
            elif not covered:
                out["missed_misheard"].append(case)
    return out


# ------------------------------------------------------------------ 5. alignment

LINES = ["Nobody leaves this house tonight", "Where did you put the money",
         "Somebody called the police already", "Get the truck around back now",
         "Those windows were locked yesterday", "Listen carefully before answering",
         "Nothing about this makes sense", "Grandma never trusted strangers",
         "Pack everything into the basement", "Morning comes faster than planned"]
# (heard line, caption line): kept, hidden, softened, dropped, and misheard.
SWEAR_LINES = [
    ("what the fuck is that thing", "What the fuck is that thing?"),
    ("oh shit they found the car", "Oh, sh*t, they found the car."),
    ("get your ass over here right now", "Get your butt over here right now."),
    ("this whole plan is bullshit anyway", "This whole plan is nonsense anyway."),
    ("shut the hell up and listen", "Shut up and listen."),
    ("get some cock on that seam", "Get some caulk on that seam."),
    ("what a slut he is today", "What a slob he is today."),
    ("damn it we missed the train", "Damn it, we missed the train."),
    ("you son of a bitch", "You son of a gun."),
    ("put the dick in the ground", "Put the dig in the ground."),
]


def dialogue() -> tuple[list[dict], list[subtitles.Cue]]:
    heard, cues = [], []
    t = 10.0
    for n in range(90):
        if n % 3 == 1:
            spoken, shown = SWEAR_LINES[(n // 3) % len(SWEAR_LINES)]
        else:
            spoken = LINES[n % len(LINES)] + f" number{n}"
            shown = spoken
        for k, w in enumerate(spoken.split()):
            heard.append({"word": " " + w, "start": round(t + k * 0.4, 3),
                          "end": round(t + k * 0.4 + 0.3, 3)})
        cues.append(subtitles.Cue(t - 0.2, t + 0.4 * len(spoken.split()) + 0.4, shown))
        t += 0.4 * len(spoken.split()) + 1.6
    return heard, cues


def alignment_cases() -> dict:
    heard, cues = dialogue()
    matches = detections(heard)
    key = [subtitles._evidence(m, cues, M, heard)[0] for m in matches]
    end = cues[-1].end
    damage = {
        "offset +8.5 s": lambda s: s + 8.5,
        "offset -30 s": lambda s: s - 30.0,
        "drift at breaks": lambda s: s + (2.0 if s < end / 3 else 6.0 if s < 2 * end / 3 else 11.0),
        "25 -> 23.976 fps": lambda s: s * 25 / 23.976 + 1.0,
        "23.976 -> 25 fps": lambda s: s * 23.976 / 25 - 1.0,
    }
    out = {"detections": len(matches), "key": dict(zip(*[range(len(key)), key]))}
    for name, fn in damage.items():
        hurt = [subtitles.Cue(fn(c.start), fn(c.end), c.text) for c in cues]
        moved, _lo, _hi = subtitles.align(hurt, heard)
        got = [subtitles._evidence(m, moved, M, heard)[0] for m in matches]
        changed = [(matches[i].text, round(matches[i].start, 1), key[i], got[i])
                   for i in range(len(key)) if key[i] != got[i]]
        risky = [c for c in changed if c[3] == "soundalike"]
        worst = max(abs(a.start - b.start) for a, b in zip(moved, cues))
        out[name] = {"changed": changed, "risky": risky, "worst_line_error": round(worst, 2)}
        for i in range(len(key)):
            ROWS.append((f"5 alignment, {name}", matches[i].text, "", key[i], got[i]))
    return out



# ------------------------------------------------------------------ report

def main(out: str = "subtitle_stress.md") -> dict:
    summary = {}
    soft = softening_cases()
    summary["1 softenings: cases"] = sum(1 for r in ROWS if r[0].startswith("1"))
    summary["1 softenings: left in (soundalike)"] = len(soft)
    summary["1 softenings: distinct (swear, caption word) left in"] = len(
        {(f["swear"], f["sub"]) for f in soft})
    mis = mishearing_cases()
    summary["2 mishearings: dictionary sound-alikes of listed swears"] = sum(
        v["alike"] for v in mis.values())
    summary["2 mishearings: common ones run, not soundalike"] = sum(
        len(v["not_soundalike"]) for v in mis.values())
    rew = reworded_cases()
    summary["3a reworded captions: left in"] = len(rew)
    fz = fuzz_script()
    summary["3b fuzz: cases"] = fz["cases"]
    summary["3b fuzz: crashes"] = len(fz["crashes"])
    summary["3b fuzz: I1 (soundalike with nothing unheard that sounds like it)"] = len(fz["I1"])
    summary["3b fuzz: I2 (soundalike while the line has the word)"] = len(fz["I2"])
    summary["3b fuzz: real swear left in"] = len(fz["left_in_real_swear"])
    summary["3b fuzz: true mishearings left in"] = fz["states"].get(("alike", "soundalike"), 0)
    fs = fuzz_from_subtitles()
    summary["4 from_subtitles: crashes"] = len(fs["crashes"])
    for k in ("overlap", "length", "not_a_swear", "duplicate"):
        summary[f"4 from_subtitles: {k}"] = len(fs[k])
    summary["4 from_subtitles: misheard swear never placed"] = sum(
        1 for c in fs["missed_misheard"] if not c[3])
    al = alignment_cases()
    for name, v in al.items():
        if isinstance(v, dict) and "changed" in v:
            summary[f"5 alignment, {name}: verdicts changed / to soundalike"] = \
                f"{len(v['changed'])} / {len(v['risky'])}"
    lines = ["# Subtitle decision stress test", "", "| check | result |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary.items()]
    lines += ["", "## Left in: softenings", "", "| swear | caption word | where | similarity |",
              "|---|---|---|---|"]
    seen = {}
    for f in soft:
        seen.setdefault((f["swear"], f["sub"]), []).append(f["where"])
    lines += [f"| {a} | {b} | {', '.join(sorted(set(w)))} | {sounds.similarity(a, b)} |"
              for (a, b), w in sorted(seen.items())]
    lines += ["", "## Left in: reworded captions and fuzz", "", "| heard | caption | word |",
              "|---|---|---|"]
    lines += [f"| {f['heard']} | {f['caption']} | {f['word']} |" for f in rew]
    lines += [f"| {c[2]} | {c[3]} | {c[0]} ({c[1]}) |" for c in fz["left_in_real_swear"]]
    lines += ["", "## Every case", "", "| category | heard | caption | expected | got |",
              "|---|---|---|---|---|"]
    lines += ["| " + " | ".join(str(x).replace("|", "/") for x in r) + " |" for r in ROWS]
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf8")
    return summary


if __name__ == "__main__":
    for key, value in main(*sys.argv[1:2]).items():
        print(f"{key:<70} {value}")
