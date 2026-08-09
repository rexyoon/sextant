"""활동 수집.

- `git.py`  : git log 파싱, 커밋 → Activity 변환
- `note.py` : 자유 메모 → Activity 변환

중복 방지는 여기서 하지 않는다. 저장 계층이 ref 로 걸러낸다.
"""

from .git import Commit, FileChange, GitError
from .note import build_note

__all__ = ["Commit", "FileChange", "GitError", "build_note"]
