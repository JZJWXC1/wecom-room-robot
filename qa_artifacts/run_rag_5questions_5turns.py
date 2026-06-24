from __future__ import annotations

import asyncio

from qa_artifacts.run_rag_5questions_5turns_utf8 import print_summary, run


if __name__ == "__main__":
    output = asyncio.run(run())
    print_summary(output)
