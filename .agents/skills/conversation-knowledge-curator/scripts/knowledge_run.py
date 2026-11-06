#!/usr/bin/env python3
"""conversation-knowledge-curator.run.v8 的唯一公开命令入口。"""

from __future__ import annotations

import knowledge_runtime

RunError = knowledge_runtime.RunError
execute_command = knowledge_runtime.execute_command
initialize_run = knowledge_runtime.initialize_run
load_run = knowledge_runtime.load_run
main = knowledge_runtime.main


if __name__ == "__main__":
    raise SystemExit(main())
