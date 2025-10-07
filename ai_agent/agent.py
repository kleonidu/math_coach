# agent.py
"""
Safe QA+Dev Agent runner.
- works in dry-run if ANTHROPIC_API_KEY is missing
- creates a Draft PR with reports/suggestions.md
- prints detailed logs for Actions
"""
from __future__ import annotations

import os
import sys
import json
import time
import base64
import argparse
from pathlib import Path
from typing import Union
import requests
import yaml

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PLAN_PATH = BASE_DIR / "tests" / "plan_smoke.yaml"
REPORTS_DIR = BASE_DIR / "reports"
DEFAULT_PROMPT_PATH = BASE_DIR / "prompts" / "bot_system.md"

# Optional: try to import Anthropic (if available in env)
try:
    from anthropic import Anthropic
    HAS_ANTHROPIC = True
except Exception:
    HAS_ANTHROPIC = False

REPO = os.environ.get("GITHUB_REPO") or os.environ.get("GITHUB_REPOSITORY")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

prompt_override = os.environ.get("BOT_SYSTEM_PROMPT_PATH")
if prompt_override:
    BOT_PROMPT_PATH = Path(prompt_override)
else:
    BOT_PROMPT_PATH = DEFAULT_PROMPT_PATH

API_GH = "https://api.github.com"

def log(*args, **kwargs):
    print(*args, **kwargs, flush=True)

def read_yaml(path: Union[str, Path]):
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8"))

def run_stub_plan(plan_path: Union[str, Path]):
    """Simple stub runner that loads YAML and produces a fake report."""
    plan = read_yaml(plan_path)
    plan_path = Path(plan_path)
    name = plan.get("name", plan_path.stem)
    steps = plan.get("steps", [])
    results = {"plan": name, "cases": [], "passed": 0, "failed": 0}
    for i, s in enumerate(steps, start=1):
        user = s.get("user", "<no user text>")
        expect = s.get("expect_regex")
        # make a plausible reply
        reply = f"(stub reply) Ответ на: {user}"
        ok = True
        if expect and "покажи" in (user or "").lower():
            ok = True
        # simple heuristics for failing one case
        if i == len(steps) and i % 2 == 0:
            ok = False
        results["cases"].append({"idx": i, "user": user, "reply": reply, "expect": expect, "ok": ok})
        if ok:
            results["passed"] += 1
        else:
            results["failed"] += 1
    return results

def call_anthropic(system_prompt: str, user_text: str):
    """Call Anthropic if available. Returns assistant text or raises."""
    if not ANTHROPIC_KEY:
        raise RuntimeError("No ANTHROPIC_API_KEY in env")
    if not HAS_ANTHROPIC:
        raise RuntimeError("Anthropic SDK not installed in environment")
    client = Anthropic(api_key=ANTHROPIC_KEY)
    # Use messages API if available, otherwise fallback to completion
    try:
        resp = client.messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-3-7-sonnet"),
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
            max_tokens=int(os.getenv("MAX_TOKENS", "1024")),
        )
        # resp content can be a list of blocks or have `content` string
        if hasattr(resp, "content"):
            parts = []
            for block in resp.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return "\n".join(parts).strip()
        return str(resp)
    except Exception as e:
        raise

def save_report_and_create_pr(report: dict, branch_name: str, apply_patches: bool = False):
    """
    Save reports/suggestions.md and create a branch + draft PR.
    Requires GITHUB_TOKEN.
    """
    if not GITHUB_TOKEN:
        log("No GITHUB_TOKEN set — skipping PR creation. Saving local report.")
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        suggestions_path = REPORTS_DIR / "suggestions.md"
        suggestions_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"pr": None, "saved": str(suggestions_path)}

    # create branch from default (use refs API)
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    # get default branch sha
    repo = REPO or os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise RuntimeError("Repository not specified in GITHUB_REPO or GITHUB_REPOSITORY")
    log("Repo:", repo)

    # get default branch
    r = requests.get(f"{API_GH}/repos/{repo}", headers=headers)
    r.raise_for_status()
    default_branch = r.json().get("default_branch", "main")
    log("Default branch:", default_branch)
    br = requests.get(f"{API_GH}/repos/{repo}/git/ref/heads/{default_branch}", headers=headers)
    br.raise_for_status()
    base_sha = br.json()["object"]["sha"]

    new_ref = f"refs/heads/{branch_name}"
    payload = {"ref": new_ref, "sha": base_sha}
    r = requests.post(f"{API_GH}/repos/{repo}/git/refs", headers=headers, json=payload)
    if r.status_code not in (200, 201):
        # maybe branch exists — continue
        log("Branch create response:", r.status_code, r.text)
    else:
        log("Created branch", branch_name)

    # create file reports/suggestions.md in that branch
    content = f"# QA Agent report\n\nGenerated at {time.asctime()}\n\n```\n{json.dumps(report, ensure_ascii=False, indent=2)}\n```\n"
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    put_payload = {
        "message": f"chore(qaa): add suggestions report {int(time.time())}",
        "content": encoded,
        "branch": branch_name,
    }
    remote_path = "ai_agent/reports/suggestions.md"
    r = requests.put(f"{API_GH}/repos/{repo}/contents/{remote_path}", headers=headers, json=put_payload)
    if r.status_code not in (200, 201):
        log("Failed to create file in branch:", r.status_code, r.text)
    else:
        log("Created reports/suggestions.md in branch")

    # create draft PR
    pr_payload = {
        "title": f"QA: auto report {int(time.time())}",
        "head": branch_name,
        "base": default_branch,
        "body": "Auto-generated QA report. Please review.",
        "draft": True,
    }
    r = requests.post(f"{API_GH}/repos/{repo}/pulls", headers=headers, json=pr_payload)
    if r.status_code not in (200, 201):
        log("Failed to create PR:", r.status_code, r.text)
        return {"pr_error": r.text}
    pr = r.json()
    log("Created PR:", pr.get("html_url"))
    return {"pr": pr.get("html_url")}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", default=str(DEFAULT_PLAN_PATH))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--autodeploy", action="store_true")
    args = parser.parse_args()

    log("Starting agent.py")
    log("Plan:", args.plan)
    log("Anthropic key present:", bool(ANTHROPIC_KEY))
    log("Anthropic SDK installed:", HAS_ANTHROPIC)
    log("GITHUB_REPO:", REPO)
    log("GITHUB_TOKEN present:", bool(GITHUB_TOKEN))

    # Ensure reports dir exists
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # load bot system prompt if exists
    sys_prompt = ""
    if BOT_PROMPT_PATH.exists():
        sys_prompt = BOT_PROMPT_PATH.read_text(encoding="utf-8")
        log("Loaded bot system prompt length:", len(sys_prompt))

    report = None
    try:
        if ANTHROPIC_KEY and HAS_ANTHROPIC:
            # try to call LLM for first step (simple)
            try:
                log("Calling Anthropic for a sample reply...")
                sample = call_anthropic(sys_prompt, "Привет, тестовая просьба.")
                log("Anthropic sample reply:", sample[:300])
            except Exception as e:
                log("Anthropic call failed:", str(e))
            # For simplicity in this minimal agent we'll still use stub runner for full plan
            report = run_stub_plan(args.plan)
        else:
            log("Running in dry-run (stub) mode — no Anthropic calls.")
            report = run_stub_plan(args.plan)
    except Exception as e:
        log("Error running plan:", str(e))
        sys.exit(1)

    # save report locally
    rpt_path = REPORTS_DIR / f"report_{int(time.time())}.json"
    rpt_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log("Saved report:", rpt_path)

    # create branch and Draft PR
    branch_name = f"qa/dev-agent/{int(time.time())}"
    pr_info = {}
    try:
        pr_info = save_report_and_create_pr(report, branch_name, apply_patches=args.apply)
    except Exception as e:
        log("PR creation error:", str(e))

    log("Done. PR info:", pr_info)
    # exit 0
    sys.exit(0)

if __name__ == "__main__":
    main()
