#!/usr/bin/env python3
"""WeChat Backup Chat Organizer & Governance Reporter.

iTunes / Finder 비암호화 백업에서 특정 키워드에 매칭되는 WeChat 단체방 대화 데이터를 추출하고,
룰 기반 설정(rules.json)에 따라 파트너/채널/고객사별 폴더 트리로 자동 분류 및 요약 리포트를 생성합니다.

주요 기능:
1. Manifest.db 인덱싱 및 암호화/비암호화 파일 매핑
2. WCDB_Contact.sqlite / MM.sqlite Protobuf 디코딩을 통한 단체방 및 사용자 실명 복원
3. fts_message.db FTS5 전문 검색 테이블 연동을 통한 무손실 텍스트 및 첨부 파일명 복구
4. rules.json 설정에 따른 다단계 채널/고객사 분류 (제목 키워드, 참여자 서명, 사업부 오버라이드)
5. 월별 대화 텍스트(YYYY-MM.txt) 파일 분할 및 실물 첨부파일 자동 수납 (attachments/)
6. 전체 단체방 트리 구조 및 데이터 거버넌스 가이드 요약 리포트 (SUMMARY.md) 생성
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


class RuleConfig:
    """분류 및 정제 규칙을 관리하는 설정 클래스."""

    def __init__(self, rules_dict: Dict[str, Any]):
        self.target_room_keywords: List[str] = [
            k.lower() for k in rules_dict.get("target_room_keywords", ["project", "partner"])
        ]
        self.distributors: Dict[str, Dict[str, Any]] = rules_dict.get("distributors", {})
        self.hq_fixed_customers: Set[str] = set(rules_dict.get("hq_fixed_customers", []))
        self.hq_hold_keywords: List[str] = [
            k.lower() for k in rules_dict.get("hq_hold_keywords", [])
        ]
        self.customer_overrides: Dict[str, List[str]] = rules_dict.get("customer_overrides", {})
        self.room_annotations: Dict[str, str] = rules_dict.get("room_annotations", {})

    @classmethod
    def load(cls, rules_path: Path) -> RuleConfig:
        if not rules_path.exists():
            fallback = rules_path.parent / "rules.example.json"
            if fallback.exists():
                print(f"[!] '{rules_path}' 파일이 없어 기본 템플릿 '{fallback}'을 로드합니다.")
                rules_path = fallback
            else:
                print(f"[!] 규칙 파일을 찾을 수 없어 기본 빈 설정을 사용합니다.")
                return cls({})

        with open(rules_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(data)

    def classify_distributor(self, room_name: str, member_text: str = "") -> str:
        """방 제목과 참여 인원 텍스트를 교차 검증하여 채널/대리점을 판별합니다."""
        lower_title = room_name.lower()
        clean_name = room_name.strip("\"'#+ ")

        # 1차: 방 제목 키워드 검사
        for dist_name, dist_info in self.distributors.items():
            if dist_name == "HQ(direct)":
                continue
            keywords = dist_info.get("keywords", [])
            if any(kw.lower() in lower_title for kw in keywords):
                return dist_name

        # 2차: 내부방 접두어 검사
        custom = self.get_custom_customer_override(room_name)
        if custom and (custom.startswith("_사내_") or custom.startswith("_내부_")):
            return "HQ(direct)"

        # 3차: 본사 고정 계정 검사
        if custom in self.hq_fixed_customers:
            return "HQ(direct)"

        # 4차: 보류 키워드 검사
        if any(kw in lower_title for kw in self.hq_hold_keywords):
            return "HQ(direct)"

        # 5차: 대리점 추가 키워드 검사
        for dist_name, dist_info in self.distributors.items():
            if dist_name == "HQ(direct)":
                continue
            extra_kws = dist_info.get("extra_room_keywords", [])
            if any(kw.lower() in lower_title for kw in extra_kws):
                return dist_name

        # 6차: 참여 인원(Member XML 서명) 검사
        if member_text:
            lower_member = member_text.lower()
            for dist_name, dist_info in self.distributors.items():
                if dist_name == "HQ(direct)":
                    continue
                sigs = dist_info.get("member_signatures", [])
                if any(sig.lower() in lower_member for sig in sigs):
                    return dist_name

        return "HQ(direct)"

    def get_custom_customer_override(self, room_name: str) -> Optional[str]:
        """설정된 오버라이드 룰에 따라 고객사/프로젝트명을 반환합니다."""
        clean_name = room_name.strip("\"'#+ ")
        lower = clean_name.lower()

        for category, patterns in self.customer_overrides.items():
            for pat in patterns:
                if pat.lower() in lower:
                    return category
        return None

    def get_room_annotation(self, room_name: str, dist: str, cust: str) -> str:
        """대화방 주석(팀/프로젝트/역할)을 반환합니다."""
        lower = room_name.lower()
        for kw, note in self.room_annotations.items():
            if kw.lower() in lower:
                return note

        if dist != "HQ(direct)":
            alias = self.distributors.get(dist, {}).get("alias", dist)
            return f"{dist} 채널 영업/기술 협력"
        return "직접 기술 협력 / 본사 채널"


def extract_protobuf_strings(data: bytes) -> List[Tuple[int, str]]:
    """바이너리 Protobuf 데이터에서 필드 번호와 유효한 UTF-8 문자열을 추출합니다."""
    strings = []
    i = 0
    n = len(data)
    while i < n:
        byte = data[i]
        wire_type = byte & 0x07
        field_num = byte >> 3
        i += 1

        if wire_type == 0:  # Varint
            while i < n and (data[i] & 0x80):
                i += 1
            i += 1
        elif wire_type == 1:  # 64-bit
            i += 8
        elif wire_type == 2:  # Length-delimited
            length = 0
            shift = 0
            while i < n:
                b = data[i]
                i += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if i + length <= n:
                val_bytes = data[i : i + length]
                try:
                    s = val_bytes.decode("utf-8")
                    if s.isprintable() and len(s.strip()) > 0:
                        strings.append((field_num, s.strip()))
                except UnicodeDecodeError:
                    pass
                i += length
        elif wire_type == 5:  # 32-bit
            i += 4
        else:
            break
    return strings


def parse_contact_protobuf(blob: bytes) -> Dict[str, str]:
    """Friend 테이블의 Protobuf blob에서 연락처 메타데이터를 파싱합니다."""
    res = {}
    if not blob:
        return res
    for fnum, s in extract_protobuf_strings(blob):
        if not s or len(s) < 2:
            continue
        if s.startswith("wxid_") and "wxid" not in res:
            res["wxid"] = s
        elif "@" not in s and "http" not in s and "/" not in s and "\\" not in s:
            if fnum in (1, 2) and "nickname" not in res:
                res["nickname"] = s
            elif fnum in (3, 4) and "remark" not in res:
                res["remark"] = s
            elif "other" not in res and fnum not in (1, 2, 3, 4):
                res["other"] = s
    return res


def clean_corrupt_message(raw_msg: Any, mtype: int = 1, preserved_filename: Optional[str] = None) -> str:
    """원시 메시지 데이터를 정제하고 읽을 수 있는 텍스트로 변환합니다."""
    if raw_msg is None:
        return ""

    if isinstance(raw_msg, bytes):
        if raw_msg.startswith(b"\x28\xb5\x2f\xfd") or mtype != 1:
            if mtype == 1:
                text = raw_msg.decode("utf-8", errors="ignore")
                text = re.sub(r"^[\x00-\x20\(\/\\]+", "", text)
                text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
                return text.replace("\n", " ").strip()
            elif mtype == 3:
                return "[이미지 파일]"
            elif mtype == 34:
                return "[음성 메시지]"
            elif mtype == 43:
                return "[동영상 파일]"
            elif mtype == 47:
                return "[이모티콘]"
            elif mtype == 49:
                if preserved_filename:
                    return f"[첨부 파일: attachments/{preserved_filename} (실물 보존)]"
                return "[첨부 파일 / 공유 문서]"
            elif mtype == 10002:
                return "[시스템 메시지]"
            else:
                if preserved_filename:
                    return f"[첨부 파일: attachments/{preserved_filename} (실물 보존)]"
                return f"[기타 미디어 (Type {mtype})]"
        else:
            msg = raw_msg.decode("utf-8", errors="ignore")
    else:
        msg = str(raw_msg)

    clean_msg = msg.replace("\n", " ").strip()
    if clean_msg.startswith("<?xml") or "<msg>" in clean_msg:
        if "<img " in clean_msg:
            return "[이미지 파일]"
        elif "<appmsg" in clean_msg:
            if preserved_filename:
                return f"[첨부 파일: attachments/{preserved_filename} (실물 보존)]"
            return "[공유 문서/카드/파일]"
        else:
            return "[미디어/특수 메시지]"

    clean_msg = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", clean_msg)
    return clean_msg.strip()


class WechatChatOrganizer:
    """WeChat 백업 분석 및 대화방 자동 정리기."""

    def __init__(self, backup_dir: Path, output_dir: Path, config: RuleConfig):
        self.backup_dir = backup_dir
        self.output_dir = output_dir
        self.config = config
        self.files_map: Dict[str, str] = {}
        self.target_rooms: Dict[str, str] = {}  # chat_hash -> room_name
        self.room_user_names: Dict[str, str] = {}
        self.room_member_texts: Dict[str, str] = {}
        self.user_display_names: Dict[str, str] = {}
        self.fts_cache: Dict[Tuple[str, int], str] = {}
        self.room_attachments: Dict[str, Dict[str, Tuple[str, Path]]] = defaultdict(dict)

    def index_manifest(self) -> None:
        """Manifest.db에서 WeChat 관련 파일들을 인덱싱합니다."""
        manifest_path = self.backup_dir / "Manifest.db"
        if not manifest_path.exists():
            # 서브디렉토리 탐색
            subdirs = [p for p in self.backup_dir.iterdir() if p.is_dir() and (p / "Manifest.db").exists()]
            if subdirs:
                subdirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                manifest_path = subdirs[0] / "Manifest.db"
                self.backup_dir = subdirs[0]

        print(f"[*] Manifest.db 인덱싱 중: {manifest_path}")
        conn = sqlite3.connect(f"file:{manifest_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT fileID, relativePath FROM Files WHERE domain='AppDomain-com.tencent.xin'")
        for fid, rpath in cur.fetchall():
            real_path = self.backup_dir / fid[:2] / fid
            if real_path.exists():
                self.files_map[rpath] = str(real_path)
        conn.close()
        print(f"[+] 총 {len(self.files_map):,}개의 WeChat 파일 인덱싱 완료")

    def index_fts_messages(self) -> None:
        """fts_message.db에서 원본 무손실 텍스트 및 첨부 파일명을 색인합니다."""
        print("[*] FTS 전체 메시지 텍스트 색인 중...")
        fts_db_paths = [v for k, v in self.files_map.items() if k.endswith("fts/fts_message.db")]
        for f_path in fts_db_paths:
            try:
                conn = sqlite3.connect(f_path)
                cur = conn.cursor()
                try:
                    cur.execute("SELECT UsrName, usernameid FROM fts_username_id")
                    room_map = {uid: hashlib.md5(u.encode("utf-8")).hexdigest() for u, uid in cur.fetchall()}
                    for i in range(4):
                        cur.execute(f"SELECT usernameid, MesLocalID, AMessage FROM fts5_message_table_{i}")
                        for uid, mid, amsg in cur.fetchall():
                            if uid in room_map and amsg:
                                self.fts_cache[(room_map[uid], mid)] = amsg
                except Exception:
                    pass
                conn.close()
            except Exception:
                pass
        print(f"[+] 총 {len(self.fts_cache):,}개의 무손실 FTS 텍스트 캐시 구축 완료!")

    def discover_chatrooms(self) -> None:
        """연락처 DB에서 설정된 키워드가 포함된 단체방(@chatroom)을 색인합니다."""
        print("[*] 대상 단체방(@chatroom) 필터링 중...")
        contact_db_paths = [
            v for k, v in self.files_map.items()
            if k.endswith("DB/WCDB_Contact.sqlite") or k.endswith("DB/MM.sqlite")
        ]

        keywords = self.config.target_room_keywords

        for c_path in contact_db_paths:
            try:
                conn = sqlite3.connect(f"file:{c_path}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute(
                    "SELECT userName, dbContactRemark, dbContactChatRoom, dbContactOther FROM Friend WHERE userName LIKE '%@chatroom'"
                )
                for uname, remark, chatroom, other in cur.fetchall():
                    all_b = b""
                    for b in (remark, chatroom, other):
                        if b:
                            all_b += b
                    text = all_b.decode("utf-8", errors="ignore")
                    lower_text = text.lower()
                    if any(kw in lower_text for kw in keywords):
                        matches = re.findall(r"[\u4e00-\u9fff\w\s&+\-·()（）\uff06#@\.\'\"]{2,}", text)
                        clean = [
                            m.strip() for m in matches
                            if "wxid_" not in m and "RoomData" not in m and "Member" not in m and len(m.strip()) > 1
                        ]
                        room_name = clean[0] if clean else uname
                        chat_hash = hashlib.md5(uname.encode("utf-8")).hexdigest()
                        self.target_rooms[chat_hash] = room_name
                        self.room_user_names[chat_hash] = uname
                        self.room_member_texts[chat_hash] = (chatroom or b"").decode("utf-8", errors="ignore")
                conn.close()
            except Exception as e:
                print(f"[!] Contact DB 읽기 오류 ({c_path}): {e}")

        print(f"[+] 총 {len(self.target_rooms)}개의 대상 단체방 식별 완료!")

    def index_user_names(self) -> None:
        """연락처 DB에서 사용자 ID와 사람이 읽을 수 있는 실명/닉네임을 색인합니다."""
        print("[*] 사용자 실명 및 닉네임 색인 중...")
        contact_db_paths = [
            v for k, v in self.files_map.items()
            if k.endswith("DB/WCDB_Contact.sqlite") or k.endswith("DB/MM.sqlite")
        ]
        for c_path in contact_db_paths:
            try:
                conn = sqlite3.connect(f"file:{c_path}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute("SELECT userName, dbContactRemark, dbContactProfile FROM Friend")
                for uname, remark, profile in cur.fetchall():
                    if not uname:
                        continue
                    info = {}
                    if remark:
                        info.update(parse_contact_protobuf(remark))
                    if profile:
                        info.update(parse_contact_protobuf(profile))

                    readable = info.get("remark") or info.get("nickname") or info.get("other")
                    if readable and len(readable.strip()) > 0:
                        self.user_display_names[uname] = readable.strip()
                conn.close()
            except Exception:
                pass
        print(f"[+] 총 {len(self.user_display_names):,}명의 사용자 실명/닉네임 색인 완료")

    def index_attachments(self) -> None:
        """백업 파일 중 실제 첨부파일을 색인합니다."""
        print("[*] 실제 다운로드된 첨부파일 인덱싱 중...")
        for rpath, real_path in self.files_map.items():
            if "OpenData" in rpath or "Attachment" in rpath:
                parts = rpath.split("/")
                filename = parts[-1]
                if len(parts) >= 3:
                    chat_hash = parts[-3] if "OpenData" in rpath else parts[-2]
                    self.room_attachments[chat_hash][filename] = (filename, Path(real_path))

    def clean_customer_name(self, room_name: str, distributor: str) -> str:
        """방 제목에서 고객사명을 추출하고 불필요한 단어를 제거합니다."""
        custom = self.config.get_custom_customer_override(room_name)
        if custom:
            return custom

        clean = room_name.strip("\"'#+ ")

        # 채널 키워드 제거
        for kw in self.config.distributors.get(distributor, {}).get("keywords", []):
            clean = re.sub(re.escape(kw), " ", clean, flags=re.IGNORECASE)

        # 불필요한 공통 단어 제거
        remove_words = [
            "技术交流群", "技术支持群", "交流群", "沟通群", "技术交流", "技术支持",
            "产品交流", "合作沟通群", "合作群", "评测群", "项目", "测试群", "支持", "交流", "沟通", "合作", "群"
        ]
        for w in remove_words:
            clean = re.sub(re.escape(w), " ", clean, flags=re.IGNORECASE)

        clean = re.sub(r"[\uff06\uff08\uff09\uff1a\uff1b\uff0c\u3002\u3001&+\-,·/|（）()\[\]【】_#@\.\'\"~～—*\^%$!?:;<>]", " ", clean)
        clean = re.sub(r"^\s*\d+\s*", "", clean)
        clean = re.sub(r"\s+", " ", clean).strip()

        if not clean or len(clean) < 2:
            return re.sub(r'[\\/*?:"<>|]', "", room_name).strip()[:20]

        customer = clean.split()[0]
        return re.sub(r'[\\/*?:"<>|]', "", customer)

    def process_and_export(self) -> None:
        """대화방 메시지를 추출하여 분류 폴더에 저장합니다."""
        print(f"[*] 출력 디렉토리 초기화: {self.output_dir}")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.index_fts_messages()
        self.index_user_names()
        self.index_attachments()

        total_exported = 0
        room_counts = defaultdict(int)

        for chat_hash, room_name in self.target_rooms.items():
            dist = self.config.classify_distributor(room_name, self.room_member_texts.get(chat_hash, ""))
            cust = self.clean_customer_name(room_name, dist)
            target_folder = self.output_dir / dist / cust
            target_folder.mkdir(parents=True, exist_ok=True)

            # 첨부파일 복사
            att_folder = target_folder / "attachments"
            if chat_hash in self.room_attachments:
                att_folder.mkdir(parents=True, exist_ok=True)
                for fname, (_, real_p) in self.room_attachments[chat_hash].items():
                    dest_p = att_folder / fname
                    if not dest_p.exists() and real_p.exists():
                        shutil.copy2(real_p, dest_p)

            # 메시지 테이블 탐색
            msg_dbs = [v for k, v in self.files_map.items() if "DB/message_" in k and k.endswith(".sqlite")]
            room_messages = []

            for mdb in msg_dbs:
                try:
                    conn = sqlite3.connect(f"file:{mdb}?mode=ro", uri=True)
                    cur = conn.cursor()
                    table_name = f"Chat_{chat_hash}"
                    cur.execute(f"SELECT CreateTime, Message, Des, Type, MesLocalID FROM {table_name}")
                    for ctime, raw_msg, des, mtype, local_id in cur.fetchall():
                        # FTS 캐시 확인
                        cached_text = self.fts_cache.get((chat_hash, local_id))
                        if cached_text:
                            msg_text = cached_text.replace("\n", " ").strip()
                        else:
                            msg_text = clean_corrupt_message(raw_msg, mtype)

                        if msg_text:
                            dt = datetime.datetime.fromtimestamp(ctime)
                            sender_display = "Me" if des == 0 else "Partner"
                            room_messages.append((dt, sender_display, msg_text))
                    conn.close()
                except Exception:
                    pass

            # 정렬 및 월별 저장
            room_messages.sort(key=lambda x: x[0])
            monthly_groups = defaultdict(list)
            for dt, sender, msg in room_messages:
                month_key = dt.strftime("%Y-%m")
                monthly_groups[month_key].append(f"[{dt.strftime('%Y-%m-%d %H:%M:%S')}] [{sender}] {msg}")

            for m_key, lines in monthly_groups.items():
                out_file = target_folder / f"{m_key}.txt"
                with open(out_file, "a", encoding="utf-8") as f:
                    f.write(f"============================================================\n")
                    f.write(f"채널/대리점: {dist}\n")
                    f.write(f"고객사/주제: {cust}\n")
                    f.write(f"단 체 방 명: {room_name}\n")
                    f.write(f"기       간: {m_key}\n")
                    f.write(f"메 시 지 수: {len(lines)}개\n")
                    f.write(f"============================================================\n\n")
                    f.write("\n".join(lines) + "\n\n")

            msg_cnt = len(room_messages)
            total_exported += msg_cnt
            room_counts[dist] += msg_cnt
            print(f"[EXPORT] [{dist}] {cust} - '{room_name}': {msg_cnt}건")

        print(f"\n[DONE] 총 {total_exported:,}건의 메시지 내보내기 완료!")
        self.generate_summary_report(total_exported)

    def generate_summary_report(self, total_exported: int) -> None:
        """대리점별 고객사/단체방 트리 구조 및 첨부파일 통계를 포함한 SUMMARY.md를 생성합니다."""
        summary_path = self.output_dir / "SUMMARY.md"
        print(f"[*] 요약 리포트 생성 중: {summary_path}")

        dist_groups = defaultdict(lambda: defaultdict(list))
        dist_attachment_counts = defaultdict(int)

        for chat_hash, room_name in self.target_rooms.items():
            member_text = self.room_member_texts.get(chat_hash, "")
            dist = self.config.classify_distributor(room_name, member_text)
            cust = self.clean_customer_name(room_name, dist)

            att_cnt = len(self.room_attachments.get(chat_hash, {}))
            dist_attachment_counts[dist] += att_cnt

            cust_folder = self.output_dir / dist / cust
            msg_cnt = 0
            if cust_folder.exists():
                for tf in cust_folder.glob("*.txt"):
                    with open(tf, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if line.startswith("[202"):
                                msg_cnt += 1

            note = self.config.get_room_annotation(room_name, dist, cust)
            dist_groups[dist][cust].append({
                "room_name": room_name,
                "count": msg_cnt,
                "att_count": att_cnt,
                "note": note,
            })

        dist_order = [d for d in self.config.distributors if d != "HQ(direct)"] + ["HQ(direct)"]
        total_attachments = sum(dist_attachment_counts.values())

        lines = [
            "# WeChat 대화 데이터 채널별 고객사/단체방 트리 구조 요약 리포트\n",
            f"- **생성 일시**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- **총 수집 메시지**: **{total_exported:,}건**",
            f"- **총 수집 단체방**: **{len(self.target_rooms)}개** (개인 1:1 대화 제외, 단체방만 분류)",
            f"- **보존된 실물 첨부파일**: **총 {total_attachments}개**",
            "- **분류 규칙**: 1차(방 제목 키워드) + 2차(참여인원 서명 크로스 검증) + 3차(사업부/TF 오버라이드)",
            "- **데이터 정제**: 무손실 FTS5 텍스트 본문 복원, 인코딩 정제, 첨부파일 실물 링크\n",
            "## 1. 채널/대리점별 요약 현황\n",
            "| 채널 / 대리점 (Distributor) | 대화방 수 | 총 메시지 수 | 보존 첨부파일 | 관리 고객사 수 |",
            "| :--- | :---: | :---: | :---: | :---: |",
        ]

        for d in dist_order:
            custs = dist_groups[d]
            total_r = sum(len(v) for v in custs.values())
            total_m = sum(sum(item["count"] for item in v) for v in custs.values())
            att_m = dist_attachment_counts[d]
            alias = self.config.distributors.get(d, {}).get("alias", d)
            lines.append(f"| **{alias}** | **{total_r}개** | **{total_m:,}건** | **{att_m}개** | **{len(custs)}개사** |")

        lines.append("\n---\n")
        lines.append("## 2. 채널별 세부 고객사 및 단체방 트리 구조 (Tree Structure)\n")

        for idx, d in enumerate(dist_order, 1):
            custs = dist_groups[d]
            total_r = sum(len(v) for v in custs.values())
            total_m = sum(sum(item["count"] for item in v) for v in custs.values())
            att_m = dist_attachment_counts[d]
            alias = self.config.distributors.get(d, {}).get("alias", d)

            lines.append(f"### {idx}. {alias}")
            lines.append(f"> **총 {total_r}개 방 | {total_m:,}건의 메시지 | 보존 첨부파일 {att_m}개**\n")
            lines.append("```text")
            lines.append(f"{d}")

            cust_keys = sorted(custs.keys())
            for c_idx, c in enumerate(cust_keys):
                is_last_c = c_idx == len(cust_keys) - 1
                c_prefix = "└── " if is_last_c else "├── "
                c_indent = "    " if is_last_c else "│   "

                rooms = sorted(custs[c], key=lambda x: x["count"], reverse=True)
                c_total_msgs = sum(r["count"] for r in rooms)
                c_total_att = sum(r["att_count"] for r in rooms)
                att_str = f", 첨부파일 {c_total_att}개" if c_total_att > 0 else ""

                lines.append(f"{c_prefix}[{c}] (총 {len(rooms)}개 방, {c_total_msgs:,}건{att_str})")
                for r_idx, r in enumerate(rooms):
                    is_last_r = r_idx == len(rooms) - 1
                    r_prefix = "└── " if is_last_r else "├── "
                    rn = r["room_name"]
                    cnt = r["count"]
                    note = r["note"]
                    r_att = r["att_count"]
                    r_att_str = f" [첨부 {r_att}개]" if r_att > 0 else ""
                    lines.append(f"{c_indent}{r_prefix}💬 \"{rn}\" [{cnt:,}건{r_att_str}] — {note}")

            lines.append("```\n")

        # 거버넌스 가이드 추가
        lines.append("---\n")
        lines.append("## 3. 데이터 거버넌스 및 표준 단체방 명명 규칙 가이드\n")
        lines.append("위챗 단체방 생성 시 아래의 **표준 규칙**을 준수하면 정규식 기반 자동 DB 인덱싱이 가능해집니다:\n")
        lines.append("```text")
        lines.append("[권장 표준 단체방 제목 포맷]")
        lines.append("{대리점/채널구분}_{고객사명}_{프로젝트/사업부/용도}")
        lines.append("```\n")

        summary_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"[SUCCESS] 요약 리포트 생성 완료: {summary_path}")


def main():
    parser = argparse.ArgumentParser(description="WeChat Backup Chat Organizer")
    default_backup = Path.home() / "Library" / "Application Support" / "MobileSync" / "Backup"
    parser.add_argument("--backup-dir", type=Path, default=default_backup, help=f"iOS 백업 폴더 경로 (기본: {default_backup})")
    parser.add_argument("--output-dir", type=Path, default=Path("./output_chats"), help="출력 디렉토리 (기본: ./output_chats)")
    parser.add_argument("--rules", type=Path, default=Path("rules.json"), help="규칙 JSON 파일 경로 (기본: rules.json)")
    args = parser.parse_args()

    config = RuleConfig.load(args.rules)
    organizer = WechatChatOrganizer(args.backup_dir, args.output_dir, config)
    organizer.index_manifest()
    organizer.discover_chatrooms()
    organizer.process_and_export()


if __name__ == "__main__":
    main()
