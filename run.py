#!/usr/bin/env python
"""Drive the agent at a goal.

  ./run.py "how many items are in the cart?" fixtures/cart.html
  ./run.py --snapshot fixtures/cart.html        # print the observation only, no model

Chrome must already be listening on :9222 (see README.md).
"""
import argparse
import asyncio
import logging
import os
import sys

from agent import agent, cdp, consent, snapshot


def to_url(target):
    if "://" in target:
        return target
    return "file://" + os.path.abspath(target)


async def show_snapshot(url):
    tid, ws = await cdp.open_url(url)
    try:
        async with cdp.Client(ws) as c:
            await asyncio.sleep(0.4)
            await snapshot.settle(c)
            env = await snapshot.build(c)
            print(f"url:   {env.get('url')}")
            print(f"title: {env.get('title')}")
            print(f"stats: {env.get('stats')}  truncated={env.get('truncated')}")
            if env.get("errors"):
                print(f"errors: {env['errors']}")
            print("\n" + snapshot.render(env))
    finally:
        await cdp.close_target(tid)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("goal", nargs="?", help="what the agent should do")
    ap.add_argument("target", help="URL or path to an HTML file")
    ap.add_argument("--snapshot", action="store_true", help="print the observation and exit")
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--allow", action="append", help="host allowed for navigate (repeatable)")
    ap.add_argument("--confirm", choices=consent.POLICIES, default=consent.DESTRUCTIVE,
                    help="when to ask a human before a mutating action "
                         "(auto=never, destructive=on irreversible wording [default], "
                         "writes=every mutation, readonly=refuse all mutations)")
    ap.add_argument("--no-eval-js", action="store_true",
                    help="disable model-authored JavaScript — recommended for "
                         "authenticated sessions, where it is the widest hole")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    url = to_url(a.target)
    if a.snapshot:
        await show_snapshot(url)
        return 0

    if not a.goal:
        print("a goal is required unless --snapshot is given", file=sys.stderr)
        return 2

    answer, hist = await agent.run(a.goal, url, a.steps, a.allow,
                                   policy=a.confirm, allow_eval_js=not a.no_eval_js)
    print(f"\n=== GOAL   : {a.goal}")
    print(f"=== ANSWER : {answer!r}")
    print(f"=== STEPS  : {len(hist)}")
    return 0 if answer is not None else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
