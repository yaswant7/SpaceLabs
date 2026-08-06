#!/usr/bin/env python3
"""Could a stranger clone this repo and point it at their own documents?

That is the product question, and it is not answered by "it works on our corpus". These
checks are the ones that fail when a codebase has quietly grown around the first customer
it ever had:

  * no shipped prompt names the organisation it was written for, or anyone who works there
  * the assistant's own name and its organisation come from settings, not from source
  * nothing half-substituted ever reaches the model — a visible %%TOKEN%% is a defect
  * the tuning constants are declared in config, not scattered through the modules that
    use them, so a new deployment has one place to look
"""
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sb import config, prompts   # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


TEMPLATES = [n for n in dir(prompts)
             if n.isupper() and isinstance(getattr(prompts, n), str)]

# Names from the corpus this was originally built against. None may appear in a prompt.
FIRST_CUSTOMER = ["spacelabs", "yaswanth", "kamineni", "meena", "sreedhar", "acme",
                  "globex", "initech", "jntuk", "modernsignal", "arjun", "priya",
                  "norvale", "okonkwo"]

print("== no shipped prompt names the first customer ==")
for name in TEMPLATES:
    body = getattr(prompts, name).lower()
    hits = [n for n in FIRST_CUSTOMER if n in body]
    if hits:
        check(f"{name} is free of real names", False, str(hits))
print(f"  checked {len(TEMPLATES)} prompt templates")
if not fails:
    check("all prompt templates are name-free", True)

print("\n== identity comes from settings ==")
check("org_name is a setting", "org_name" in config.ENV_DEFAULTS)
check("assistant_name is a setting", "assistant_name" in config.ENV_DEFAULTS)

rendered = prompts.render(prompts.RAG_SYSTEM)
check("render() substitutes the assistant name",
      "%%BOT%%" not in rendered and "%%ORG%%" not in rendered)

print("\n== no template leaks a placeholder once rendered ==")
leaky = []
for name in TEMPLATES:
    out = prompts.render(getattr(prompts, name))
    if re.search(r"%%[A-Z_]+%%", out):
        leaky.append(name)
check("nothing renders with a %%TOKEN%% still in it", not leaky, str(leaky))

print("\n== an unnamed deployment still reads naturally ==")
# A fresh clone has set nothing. The prompt must not say "You are the assistant for ."
blank = prompts._ORG_TOKEN in prompts.RAG_SYSTEM
check("RAG_SYSTEM is written against the org token", blank)
neutral = prompts.RAG_SYSTEM.replace(prompts._BOT_TOKEN, "the assistant") \
                            .replace(prompts._ORG_TOKEN, "your team")
check("neutral fallback produces no empty gaps",
      "  " not in neutral.split("\n")[0] and " ." not in neutral.split("\n")[0],
      neutral.split("\n")[0][:70])

print("\n== tuning constants live in config, not buried in modules ==")
for knob in ("CHUNK_CHARS", "RETRIEVE_CHUNKS", "RRF_K", "RELATIVE_CUTOFF",
             "SUBJECT_BOOST", "EVIDENCE_STRONG", "EVIDENCE_WEAK", "EMBED_MODEL"):
    check(f"config.{knob}", hasattr(config, knob))

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
