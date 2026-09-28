#!/usr/bin/env python3
"""iOS WeChat Backup Extractor.

iTunes/Finder의 비암호화 백업에서 WeChat 대화 데이터를 직접 추출하여
텍스트 및 정규화된 포맷으로 변환합니다.

주요 기능:
- Manifest.db 자동 탐색 및 WeChat(AppDomain-com.tencent.xin) 파일 인덱싱
- MM.sqlite 및 session.db를 통한 대화방 이름/친구 목록 매핑
- message_*.sqlite의 Chat_<hash> 테이블에서 메시지 추출
- 키워드 필터링 (예: 특정 프로젝트명 등)
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def md5_hex(s: str) -> str:
    """문자열의 MD5 해시값을 반환합니다."""
    return hashlib.md5(s.encode("utf-8")).hexdigest()


class IOSWeChatBackupExtractor:
    def __init__(self, backup_dir: Path):
        self.backup_dir = backup_dir
        self.manifest_db_path = backup_dir / "Manifest.db"
        self.wechat_files: Dict[str, str] = {}  # relativePath -> realFilePath
        self.contacts: Dict[str, Dict[str, Any]] = {}  # hash/userName -> info
        self.sessions: List[Dict[str, Any]] = []

    def validate_backup(self) -> bool:
        """백업 디렉토리와 Manifest.db 존재 여부를 검증합니다."""
        if not self.backup_dir.exists():
            print(f"[ERROR] 백업 디렉토리가 존재하지 않습니다: {self.backup_dir}")
            return False
        if not self.manifest_db_path.exists():
            print(f"[ERROR] Manifest.db를 찾을 수 없습니다. (백업 진행 중이거나 손상됨): {self.manifest_db_path}")
            return False
        return True

    def index_wechat_files(self) -> int:
        """Manifest.db에서 WeChat 관련 파일들을 인덱싱합니다."""
        print(f"[*] Manifest.db 인덱싱 중: {self.manifest_db_path}")
        conn = sqlite3.connect(f"file:{self.manifest_db_path}?mode=ro", uri=True)
        cursor = conn.cursor()

        # iTunes 백업 파일 매핑
        # fileID의 앞 2글자가 하위 디렉토리명 (예: 0a/0a12bc...)
        query = """
            SELECT fileID, relativePath 
            FROM Files 
            WHERE domain = 'AppDomain-com.tencent.xin' AND flags = 1
        """
        count = 0
        try:
            cursor.execute(query)
            for file_id, rel_path in cursor.fetchall():
                real_path = self.backup_dir / file_id[:2] / file_id
                if real_path.exists():
                    self.wechat_files[rel_path] = str(real_path)
                    count += 1
        except sqlite3.Error as e:
            print(f"[ERROR] Manifest.db 쿼리 실패: {e}")
        finally:
            conn.close()

        print(f"[+] 총 {count}개의 WeChat 파일 인덱싱 완료")
        return count

    def find_user_directories(self) -> List[str]:
        """Documents 아래의 32자리 사용자 해시 디렉토리 목록을 찾습니다."""
        user_dirs = set()
        for rel_path in self.wechat_files:
            if rel_path.startswith("Documents/"):
                parts = rel_path.split("/")
                if len(parts) >= 2 and len(parts[1]) == 32:
                    user_dirs.add(parts[1])
        return sorted(list(user_dirs))

    def load_contacts_and_sessions(self, user_hash: str) -> None:
        """사용자 폴더 내의 MM.sqlite 및 session.db에서 대화방/친구 이름을 로드합니다."""
        # 1. MM.sqlite 찾기
        mm_sqlite_rel = f"Documents/{user_hash}/DB/MM.sqlite"
        mm_real_path = self.wechat_files.get(mm_sqlite_rel)

        if mm_real_path and os.path.exists(mm_real_path):
            print(f"[*] 연락처/대화방 DB 로드 중: {mm_sqlite_rel}")
            try:
                conn = sqlite3.connect(f"file:{mm_real_path}?mode=ro", uri=True)
                cursor = conn.cursor()
                cursor.execute("SELECT userName, dbContactRemark, dbContactChatRoom, type FROM Friend")
                for username, remark_blob, chatroom_blob, u_type in cursor.fetchall():
                    h = md5_hex(username)
                    nickname = username
                    remark = ""
                    # 간단한 blob 파싱 (추후 필요시 확장)
                    self.contacts[h] = {
                        "userName": username,
                        "nickname": nickname,
                        "remark": remark,
                        "hash": h,
                        "type": u_type
                    }
                conn.close()
            except Exception as e:
                print(f"[WARNING] MM.sqlite 읽기 실패: {e}")

        # 2. session.db 찾기
        session_rel = f"Documents/{user_hash}/session/session.db"
        session_real_path = self.wechat_files.get(session_rel)
        if session_real_path and os.path.exists(session_real_path):
            print(f"[*] 세션 목록 로드 중: {session_rel}")
            try:
                conn = sqlite3.connect(f"file:{session_real_path}?mode=ro", uri=True)
                cursor = conn.cursor()
                # SessionAbstract 또는 Session 테이블
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [r[0] for r in cursor.fetchall()]
                target_table = "SessionAbstract" if "SessionAbstract" in tables else ("Session" if "Session" in tables else None)
                if target_table:
                    cursor.execute(f"SELECT * FROM {target_table} LIMIT 5")
                    # 세션 기본 정보 수집
                conn.close()
            except Exception as e:
                print(f"[WARNING] session.db 읽기 실패: {e}")

    def extract_messages(self, user_hash: str, keyword_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """message_*.sqlite 파일들에서 대화 메시지를 추출합니다."""
        results = []
        msg_db_files = []
        prefix = f"Documents/{user_hash}/DB/message_"
        for rel_path, real_path in self.wechat_files.items():
            if rel_path.startswith(prefix) and rel_path.endswith(".sqlite"):
                msg_db_files.append((rel_path, real_path))

        print(f"[*] 총 {len(msg_db_files)}개의 메시지 DB 발견")
        for rel_path, real_path in sorted(msg_db_files):
            try:
                conn = sqlite3.connect(f"file:{real_path}?mode=ro", uri=True)
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Chat_%'")
                chat_tables = [r[0] for r in cursor.fetchall()]

                for table in chat_tables:
                    chat_hash = table[5:]  # strip 'Chat_'
                    # 필터 확인
                    room_info = self.contacts.get(chat_hash, {})
                    room_name = room_info.get("userName", chat_hash)

                    if keyword_filter and keyword_filter.lower() not in room_name.lower():
                        continue

                    cursor.execute(f"SELECT CreateTime, Des, Message, Type FROM {table} ORDER BY CreateTime ASC")
                    for create_time, des, msg, m_type in cursor.fetchall():
                        if not msg:
                            continue
                        dt = datetime.datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S")
                        results.append({
                            "room_hash": chat_hash,
                            "room_name": room_name,
                            "timestamp": dt,
                            "epoch": create_time,
                            "is_sent": (des == 0),
                            "type": m_type,
                            "message": msg
                        })
                conn.close()
            except Exception as e:
                print(f"[WARNING] 메시지 DB 처리 중 에러 ({rel_path}): {e}")

        return results


def main():
    parser = argparse.ArgumentParser(description="iOS WeChat Backup Extractor")
    default_backup = Path.home() / "Library" / "Application Support" / "MobileSync" / "Backup"
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=default_backup,
        help=f"iOS 백업 폴더 경로 (기본: {default_backup})"
    )
    parser.add_argument("--filter", type=str, default="", help="대화방 이름 필터 (예: project, team)")
    parser.add_argument("--output", type=Path, default=Path("extracted_messages.txt"), help="출력 파일 경로")

    args = parser.parse_args()

    # 최신 백업 탐색
    backup_path = args.backup_dir
    if not (backup_path / "Manifest.db").exists():
        subdirs = [p for p in backup_path.iterdir() if p.is_dir() and not p.name.startswith(".")]
        if subdirs:
            # 가장 최근 수정된 폴더 선택
            subdirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            backup_path = subdirs[0]
            print(f"[*] 최신 백업 폴더 자동 선택: {backup_path}")

    extractor = IOSWeChatBackupExtractor(backup_path)
    if not extractor.validate_backup():
        sys.exit(1)

    extractor.index_wechat_files()
    user_dirs = extractor.find_user_directories()
    if not user_dirs:
        print("[!] WeChat 사용자 디렉토리를 찾을 수 없습니다.")
        sys.exit(1)

    print(f"[+] 발견된 WeChat 사용자: {user_dirs}")
    for u in user_dirs:
        extractor.load_contacts_and_sessions(u)
        messages = extractor.extract_messages(u, keyword_filter=args.filter)
        print(f"[+] 추출된 메시지 수: {len(messages)} 건")

        if messages:
            with open(args.output, "w", encoding="utf-8") as f:
                for m in messages:
                    sender = "Me" if m["is_sent"] else "Other"
                    f.write(f"[{m['timestamp']}] ({m['room_name']}) {sender}: {m['message']}\n")
            print(f"[SUCCESS] 결과 저장 완료: {args.output}")


if __name__ == "__main__":
    main()
