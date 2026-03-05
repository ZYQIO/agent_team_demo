#!/usr/bin/env python3
"""
Minimal local "agent team" demo.

Roles:
- Planner: breaks user goal into concrete steps
- Executor: performs filesystem analysis
- Reviewer: validates output quality and highlights risks
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import pathlib
from typing import Dict, List


@dataclasses.dataclass
class Plan:
    goal: str
    steps: List[str]


@dataclasses.dataclass
class ExecutionResult:
    scanned_files: int
    total_lines: int
    heading_counts: Dict[str, int]
    top_files_by_headings: List[str]


@dataclasses.dataclass
class ReviewResult:
    status: str
    notes: List[str]


class PlannerAgent:
    def run(self, goal: str) -> Plan:
        steps = [
            "Discover markdown files recursively under target directory",
            "Count lines and markdown headings per file",
            "Build a concise report with top files by heading density",
            "Run a quality check before final output",
        ]
        return Plan(goal=goal, steps=steps)


class ExecutorAgent:
    def run(self, target: pathlib.Path) -> ExecutionResult:
        md_files = sorted(p for p in target.rglob("*.md") if p.is_file())
        heading_counts: Dict[str, int] = {}
        total_lines = 0

        for md in md_files:
            text = md.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()
            total_lines += len(lines)
            headings = sum(1 for line in lines if line.lstrip().startswith("#"))
            heading_counts[str(md)] = headings

        top_files = sorted(
            heading_counts.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )[:5]
        top_files_by_headings = [f"{path} (headings={count})" for path, count in top_files]

        return ExecutionResult(
            scanned_files=len(md_files),
            total_lines=total_lines,
            heading_counts=heading_counts,
            top_files_by_headings=top_files_by_headings,
        )


class ReviewerAgent:
    def run(self, result: ExecutionResult) -> ReviewResult:
        notes: List[str] = []

        if result.scanned_files == 0:
            return ReviewResult(status="fail", notes=["No markdown files found."])

        if result.total_lines == 0:
            notes.append("Files discovered, but all appear empty.")

        max_headings = max(result.heading_counts.values(), default=0)
        if max_headings == 0:
            notes.append("No markdown headings detected in any file.")
        else:
            notes.append("Heading detection looks healthy.")

        notes.append(f"Analyzed {result.scanned_files} markdown files.")
        return ReviewResult(status="pass", notes=notes)


def run_demo(goal: str, target: pathlib.Path) -> None:
    planner = PlannerAgent()
    executor = ExecutorAgent()
    reviewer = ReviewerAgent()

    now = dt.datetime.now().isoformat(timespec="seconds")
    print(f"[meta] timestamp={now}")
    print(f"[meta] target={target.resolve()}")
    print()

    print("[planner] goal")
    print(f"- {goal}")
    plan = planner.run(goal)
    print("[planner] plan")
    for idx, step in enumerate(plan.steps, start=1):
        print(f"{idx}. {step}")
    print()

    exec_result = executor.run(target)
    print("[executor] summary")
    print(
        json.dumps(
            dataclasses.asdict(exec_result),
            ensure_ascii=False,
            indent=2,
        )
    )
    print()

    review_result = reviewer.run(exec_result)
    print("[reviewer] verdict")
    print(json.dumps(dataclasses.asdict(review_result), ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local multi-role agent demo.")
    parser.add_argument(
        "--goal",
        default="Summarize markdown knowledge base quality in this repo.",
        help="Natural language goal for the planner.",
    )
    parser.add_argument(
        "--target",
        default=".",
        help="Directory to analyze.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_demo(goal=args.goal, target=pathlib.Path(args.target))
