#!/usr/bin/env python3
"""UWED deadline reminder Telegram bot — no third-party Python packages required.

Data source: 08.11.2026 UWED Moodle HTML snapshots / student-group PDF.
This bot does not log into Moodle or fetch live deadlines automatically.
"""
from __future__ import annotations

import html
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
UZ_TZ = timezone(timedelta(hours=5), name='Asia/Tashkent')
LEAD_HOURS = (168, 72, 24, 6, 1)
TRACK_LABELS = {
    'common': 'Umumiy fanlar',
    'eng_ui': 'Ingliz tili — Upper-Intermediate',
    'eng_b1': 'Ingliz tili — B1',
    'eng_a2': 'Ingliz tili — A2',
    'ru_a2': 'Rus tili — A2',
    'ru_b1': 'Rus tili — B1',
}
SUPPORTED_TRACK = re.compile(r'^[a-z][a-z0-9_]{1,30}$')
LOGGER = logging.getLogger('uwed-bot')


def load_dotenv(path: Path) -> None:
    """Minimal .env reader (no pip). Never echo values."""
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        raw = raw.strip()
        if not raw or raw.startswith('#') or '=' not in raw:
            continue
        k, v = raw.split('=', 1)
        k = k.strip()
        if k not in os.environ:
            os.environ[k] = v.strip().strip('"').strip("'")


def local_now() -> datetime:
    return datetime.now(UZ_TZ)


def parse_due(value: str) -> datetime:
    # All dates in supplied records use local Tashkent time; do not infer UTC.
    return datetime.strptime(value.strip(), '%Y-%m-%d %H:%M').replace(tzinfo=UZ_TZ)


def parse_user_date(value: str) -> str:
    return parse_due(value).strftime('%Y-%m-%d %H:%M')


def display_due(value: str) -> str:
    return parse_due(value).strftime('%d.%m.%Y, %H:%M')


def remaining(due: str, now: datetime | None = None) -> str:
    now = now or local_now()
    total = int((parse_due(due) - now).total_seconds() // 60)
    if total < 0:
        return 'muddati o‘tgan'
    if total < 1:
        return '1 daqiqadan kam'
    days, rem = divmod(total, 1440)
    hours, minutes = divmod(rem, 60)
    if days:
        return f'{days} kun {hours} soat'
    if hours:
        return f'{hours} soat {minutes} daqiqa'
    return f'{minutes} daqiqa'


def label_track(track: str) -> str:
    return TRACK_LABELS.get(track, track.replace('_',' ').upper())


def label_lead(hours: int) -> str:
    return f'{hours//24} kun' if hours > 24 else f'{hours} soat'


def esc(value: str) -> str:
    return html.escape(str(value), quote=True)


def valid_url(url: str) -> bool:
    return (url == '-' or (url.startswith('https://') and len(url) < 400 and '\n' not in url))


class Store:
    def __init__(self, path: str, seed_path: Path = ROOT / 'deadlines.json', undated_path: Path = ROOT / 'undated.json'):
        self.seed = json.loads(seed_path.read_text(encoding='utf-8'))
        self.undated = json.loads(undated_path.read_text(encoding='utf-8'))
        self.seed_by_id = {e['id']: e for e in self.seed}
        assert len(self.seed_by_id) == len(self.seed)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(p))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT,
            enabled INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tracks (
            chat_id INTEGER NOT NULL, track TEXT NOT NULL,
            PRIMARY KEY (chat_id, track)
        );
        CREATE TABLE IF NOT EXISTS custom_events (
            id TEXT PRIMARY KEY, owner_chat_id INTEGER NOT NULL,
            deadline TEXT NOT NULL, subject TEXT NOT NULL,
            kind TEXT NOT NULL, topic TEXT NOT NULL,
            prep TEXT NOT NULL DEFAULT '', track TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '-'
        );
        CREATE TABLE IF NOT EXISTS overrides (
            chat_id INTEGER NOT NULL, event_id TEXT NOT NULL,
            deadline TEXT, topic TEXT, hidden INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, event_id)
        );
        CREATE TABLE IF NOT EXISTS sent (
            chat_id INTEGER NOT NULL, event_id TEXT NOT NULL,
            lead_hours INTEGER NOT NULL, deadline TEXT NOT NULL,
            sent_time TEXT NOT NULL,
            PRIMARY KEY (chat_id, event_id, lead_hours, deadline)
        );
        CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, val TEXT NOT NULL);
        ''')
        self.conn.commit()

    def register(self, chat_id: int, kind: str, title: str = '', enable: bool | None = None) -> None:
        self.conn.execute('INSERT OR IGNORE INTO chats(chat_id, kind, title, enabled) VALUES(?,?,?,0)',(chat_id,kind,title))
        self.conn.execute('UPDATE chats SET kind=?,title=? WHERE chat_id=?',(kind,title,chat_id))
        if enable is not None:
            self.conn.execute('UPDATE chats SET enabled=? WHERE chat_id=?',(int(enable),chat_id))
        self.conn.commit()

    def enabled(self, chat_id: int) -> bool:
        row=self.conn.execute('SELECT enabled FROM chats WHERE chat_id=?',(chat_id,)).fetchone()
        return bool(row['enabled']) if row else False

    def known_chats(self) -> list[sqlite3.Row]:
        return self.conn.execute('SELECT * FROM chats WHERE enabled=1').fetchall()

    def subscribed(self, chat_id: int) -> set[str]:
        return {'common'} | {row['track'] for row in self.conn.execute('SELECT track FROM tracks WHERE chat_id=?',(chat_id,))}

    def track_change(self, chat_id: int, track: str, on: bool) -> None:
        if on:
            self.conn.execute('INSERT OR IGNORE INTO tracks(chat_id,track) VALUES(?,?)',(chat_id,track))
        else:
            self.conn.execute('DELETE FROM tracks WHERE chat_id=? AND track=?',(chat_id,track))
        self.conn.commit()

    def list_events(self, chat_id: int, future_only: bool = True, now: datetime | None = None, show_hidden: bool = False) -> list[dict]:
        # Seed deadlines are public to all chats, user-added events are chat-specific.
        result=[dict(ev) for ev in self.seed]
        for row in self.conn.execute('SELECT * FROM custom_events WHERE owner_chat_id=?',(chat_id,)):
            obj=dict(row)
            obj['source']='Guruh administratori tomonidan qo‘shilgan'
            result.append(obj)
        tracks=self.subscribed(chat_id)
        result=[ev for ev in result if ev['track'] in tracks]
        for ev in result:
            row=self.conn.execute('SELECT deadline,topic,hidden FROM overrides WHERE chat_id=? AND event_id=?',(chat_id,ev['id'])).fetchone()
            if row:
                if row['deadline'] is not None: ev['deadline']=row['deadline']
                if row['topic'] is not None: ev['topic']=row['topic']
                ev['hidden']=bool(row['hidden'])
            else:
                ev['hidden']=False
        if not show_hidden:
            result=[ev for ev in result if not ev['hidden']]
        if future_only:
            n=now or local_now()
            result=[ev for ev in result if parse_due(ev['deadline']) > n]
        return sorted(result,key=lambda ev:(ev['deadline'],ev['subject'],ev['id']))

    def has_event(self, chat_id: int, event_id: str) -> bool:
        if event_id in self.seed_by_id: return True
        return self.conn.execute('SELECT 1 FROM custom_events WHERE owner_chat_id=? AND id=?',(chat_id,event_id)).fetchone() is not None

    def get_event(self, chat_id: int, event_id: str) -> dict | None:
        if event_id in self.seed_by_id:
            ev=dict(self.seed_by_id[event_id])
        else:
            row=self.conn.execute('SELECT * FROM custom_events WHERE owner_chat_id=? AND id=?',(chat_id,event_id)).fetchone()
            if not row:return None
            ev=dict(row)
        row=self.conn.execute('SELECT * FROM overrides WHERE chat_id=? AND event_id=?',(chat_id,event_id)).fetchone()
        if row:
            if row['deadline'] is not None: ev['deadline']=row['deadline']
            if row['topic'] is not None:ev['topic']=row['topic']
            ev['hidden']=bool(row['hidden'])
        return ev

    def add_event(self, chat_id: int, deadline: str, subject: str, kind: str, topic: str, track: str, url: str) -> str:
        for _ in range(10):
            event_id='C'+secrets.token_hex(3).upper()
            if not self.has_event(chat_id,event_id):break
        self.conn.execute('INSERT INTO custom_events VALUES(?,?,?,?,?,?,?,?,?)',
                          (event_id,chat_id,deadline,subject,kind,topic,'Moodle havolasidan tayyorlanish talablarini tekshiring',track,url))
        self.conn.commit()
        return event_id

    def remove_event(self, chat_id: int, event_id: str) -> bool:
        cur=self.conn.execute('DELETE FROM custom_events WHERE owner_chat_id=? AND id=?',(chat_id,event_id))
        self.conn.execute('DELETE FROM sent WHERE chat_id=? AND event_id=?',(chat_id,event_id))
        self.conn.commit()
        return cur.rowcount>0

    def override(self, chat_id: int, event_id: str, deadline: str | None = None, topic: str | None = None, hidden: bool | None = None) -> None:
        self.conn.execute('INSERT OR IGNORE INTO overrides(chat_id,event_id) VALUES (?,?)',(chat_id,event_id))
        if deadline is not None:
            self.conn.execute('UPDATE overrides SET deadline=? WHERE chat_id=? AND event_id=?',(deadline,chat_id,event_id))
        if topic is not None:
            self.conn.execute('UPDATE overrides SET topic=? WHERE chat_id=? AND event_id=?',(topic,chat_id,event_id))
        if hidden is not None:
            self.conn.execute('UPDATE overrides SET hidden=? WHERE chat_id=? AND event_id=?',(int(hidden),chat_id,event_id))
        self.conn.commit()

    def already_sent(self, chat_id: int, ev: dict, lead_hours: int) -> bool:
        return self.conn.execute('SELECT 1 FROM sent WHERE chat_id=? AND event_id=? AND lead_hours=? AND deadline=?',
                                 (chat_id,ev['id'],lead_hours,ev['deadline'])).fetchone() is not None

    def mark_sent(self, chat_id: int, evs: list[dict], lead_hours: int, now: datetime) -> None:
        self.conn.executemany('INSERT OR IGNORE INTO sent VALUES(?,?,?,?,?)',
            [(chat_id,ev['id'],lead_hours,ev['deadline'],now.isoformat()) for ev in evs])
        self.conn.commit()

    def get_offset(self) -> int:
        row=self.conn.execute("SELECT val FROM kv WHERE key='update_offset'").fetchone()
        return int(row['val']) if row else 0

    def set_offset(self, offset: int) -> None:
        self.conn.execute("INSERT INTO kv(key,val) VALUES('update_offset',?) ON CONFLICT(key) DO UPDATE SET val=excluded.val",(str(offset),))
        self.conn.commit()


class TelegramAPI:
    def __init__(self, token: str):
        self.base='https://api.telegram.org/bot'+token+'/'

    def call(self, method: str, **kwargs):
        params={k:(json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v) for k,v in kwargs.items() if v is not None}
        body=urlencode(params).encode('utf-8')
        req=Request(self.base+method,data=body,headers={'Content-Type':'application/x-www-form-urlencoded'})
        try:
            with urlopen(req,timeout=(40 if method=='getUpdates' else 20)) as resp:
                data=json.loads(resp.read().decode('utf-8'))
        except HTTPError as exc:
            desc=exc.read().decode('utf-8',errors='replace')[:300]
            raise RuntimeError(f'Telegram API HTTP {exc.code}: {desc}') from exc
        if not data.get('ok'):
            raise RuntimeError('Telegram API: '+str(data.get('description','unknown error')))
        return data.get('result')

    def send(self, chat_id: int, text: str):
        return self.call('sendMessage',chat_id=chat_id,text=text,parse_mode='HTML',disable_web_page_preview='true')


def event_line(ev: dict, now: datetime, *, verbose: bool = True) -> str:
    code=esc(ev['id'])
    bits=[f'📚 <b>{esc(ev["subject"])}</b> · <code>{code}</code>',f'📝 {esc(ev["kind"])}',
          f'📖 Mavzu: {esc(ev["topic"])}']
    if verbose:
        bits.append(f'📌 Tayyorlanish: {esc(ev.get("prep") or "Moodle’da tekshiring")}' )
    bits.append(f'⏳ <b>{esc(display_due(ev["deadline"]))}</b> · {esc(remaining(ev["deadline"],now))} qoldi')
    if ev.get('track')!='common':bits.append(f'👥 {esc(label_track(ev["track"]))}')
    if ev.get('url','-')!='-' and valid_url(ev['url']):
        bits.append(f'🔗 <a href="{esc(ev["url"])}">Moodle topshirig‘i</a>')
    return '\n'.join(bits)


def notification_line(ev: dict, now: datetime) -> str:
    # Shorter than /next for group reminders; maintain source uncertainty.
    bits=[f'• <b>{esc(ev["subject"])}</b> — {esc(ev["kind"])}',
          f'  📖 {esc(ev["topic"])}',
          f'  📌 {esc(ev.get("prep") or "Moodle’da tekshiring")}',
          f'  🕒 {esc(display_due(ev["deadline"]))}']
    if ev['track']!='common':bits.append('  👥 '+esc(label_track(ev['track'])))
    if valid_url(ev.get('url','-')) and ev.get('url')!='-':bits.append(f'  <a href="{esc(ev["url"])}">Moodle’da ochish</a>')
    return '\n'.join(bits)


def take_chunks(lines: list[str], heading: str, max_chars: int = 3500) -> list[str]:
    chunks=[]; current=heading
    for line in lines:
        if len(current)+len(line)+2 > max_chars and current!=heading:
            chunks.append(current);current=heading
        current+='\n\n'+line
    if current!=heading or not chunks:chunks.append(current)
    return chunks


def pending_reminders(store: Store, chat_id: int, now: datetime) -> dict[int,list[dict]]:
    output={}
    for ev in store.list_events(chat_id,now=now):
        due=parse_due(ev['deadline'])
        for hours in LEAD_HOURS:
            target=due-timedelta(hours=hours)
            age=(now-target).total_seconds()
            # 10-minute grace covers a short outage, but does not flood new groups with old reminders.
            if 0 <= age < 600 and not store.already_sent(chat_id,ev,hours):
                output.setdefault(hours,[]).append(ev)
    return output


HELP_TEXT=(
    '🎓 <b>UWED Deadline Bot</b>\n'
    '2026/27 · Toshkent vaqti (UTC+5)\n\n'
    '<b>Ko‘rish:</b>\n'
    '/next — eng yaqin 5 ta deadline\n'
    '/today — keyingi 24 soat\n'
    '/week — keyingi 7 kun\n'
    '/all — barcha kelgusi deadline’lar\n'
    '/unknown — aniq sanasi yo‘q vazifalar\n'
    '/status — eslatmalar va level holati\n\n'
    '<b>Til leveli:</b>\n'
    '/track eng_ui on — Upper-Intermediate English yoqish\n'
    '/track eng_ui off — o‘chirish\n'
    'Boshqa kodlar: eng_b1, eng_a2, ru_a2, ru_b1. Ular uchun ma’lumot admin tomonidan kiritilishi kerak.\n\n'
    '<b>Guruh administratori:</b>\n'
    '/enable — avtomatik eslatmalarni yoqish\n'
    '/disable — o‘chirish\n'
    '/add — yangi deadline qo‘shish (format)\n'
    '/setdate ID YYYY-MM-DD HH:MM — sanani tuzatish\n'
    '/topic ID Yangi mavzu — mavzuni tuzatish\n'
    '/hide ID va /unhide ID — guruh uchun yashirish/qaytarish\n'
    '/remove ID — admin qo‘shgan vazifani o‘chirish\n'
    '/test — eng yaqin eslatmani ko‘rish\n\n'
    'Eslatmalar: 7 kun, 3 kun, 24 soat, 6 soat va 1 soat oldin. '
    'Bot Moodle’ga kirmaydi va yangilanishlarni avtomatik yuklab olmaydi.'
)

ADD_HELP=(
    '<b>Yangi deadline qo‘shish:</b>\n'
    '<code>/add 2026-12-04 23:59 | Rus tili | Oraliq nazorat | 6 padej | ru_a2 | https://moodle.uwed.uz/...</code>\n\n'
    'Ketma-ketlik: <b>sana vaqt | fan | nazorat turi | mavzu | level kodi | havola</b>.\n'
    'Umumiy fanlar uchun level: <code>common</code>. Havola bo‘lmasa <code>-</code>.\n'
    'Guruh uchun rus tili eslatmalarini ko‘rsatish: <code>/track ru_a2 on</code>. '
    'Faqat haqiqiy Moodle deadline’ini qo‘shing.'
)


class Bot:
    def __init__(self, api, store: Store):
        self.api=api;self.store=store

    def send(self, cid: int, message: str) -> None:
        self.api.send(cid,message)

    def send_events(self, cid: int, title: str, events: list[dict], now: datetime, cap: int | None = None):
        if cap:events=events[:cap]
        if not events:
            self.send(cid,title+'\n\nHozircha bu oraliqda belgilangan deadline yo‘q.')
            return
        lines=[event_line(e,now) for e in events]
        for chunk in take_chunks(lines,title):self.send(cid,chunk)

    def admin_allowed(self, cid: int, user_id: int, chat_kind: str) -> bool:
        if chat_kind=='private':return True
        try:
            status=self.api.call('getChatMember',chat_id=cid,user_id=user_id).get('status')
            return status in ('creator','administrator')
        except Exception as exc:
            LOGGER.warning('getChatMember failed for %s: %s',cid,exc)
            return False

    def handle_message(self, message: dict) -> None:
        raw=(message.get('text') or '').strip()
        if not raw.startswith('/'):return
        command,*rest=raw.split(maxsplit=1)
        command=command.split('@')[0].lower()
        args=rest[0].strip() if rest else ''
        ch=message['chat'];cid=int(ch['id']);kind=ch.get('type','private');uid=message.get('from',{}).get('id',0)
        title=ch.get('title') or ch.get('first_name') or ''
        # Never enable group alerts because a random group member queried /next.
        self.store.register(cid,kind,title, enable=(True if kind=='private' and command=='/start' else None))
        now=local_now()
        needs_admin={'/enable','/disable','/add','/remove','/setdate','/topic','/hide','/unhide'}
        if kind!='private' and command=='/track':needs_admin.add('/track')
        if command in needs_admin and not self.admin_allowed(cid,uid,kind):
            self.send(cid,'⛔ Bu buyruqni guruhda faqat administrator ishlatishi mumkin.')
            return
        if command in ('/start','/help'):
            msg=HELP_TEXT
            if kind!='private':msg+='\n\n🔔 Guruhga avtomatik eslatma uchun admin <code>/enable</code> deb yozishi kerak.'
            self.send(cid,msg)
        elif command=='/enable':
            self.store.register(cid,kind,title,enable=True)
            self.send(cid,'✅ Eslatmalar yoqildi. Umumiy fanlar avtomatik kuzatiladi.\n'
                          'Til guruhi kerak bo‘lsa: <code>/track eng_ui on</code>.\n'
                          'Keyingi deadline: /next')
        elif command in ('/disable','/off'):
            if kind!='private' and not self.admin_allowed(cid,uid,kind):
                self.send(cid,'⛔ Faqat guruh administratori o‘chira oladi.');return
            self.store.register(cid,kind,title,enable=False)
            self.send(cid,'🔕 Avtomatik eslatmalar o‘chirildi. /next va /week ishlayveradi.')
        elif command=='/on':
            if kind!='private':self.send(cid,'Guruhda eslatmani yoqish: /enable (admin).');return
            self.store.register(cid,kind,title,enable=True)
            self.send(cid,'✅ Shaxsiy eslatmalar yoqildi.')
        elif command in ('/next','/today','/week','/all'):
            events=self.store.list_events(cid,now=now)
            if command=='/today':events=[e for e in events if parse_due(e['deadline'])<=now+timedelta(hours=24)]
            if command=='/week':events=[e for e in events if parse_due(e['deadline'])<=now+timedelta(days=7)]
            caps={'/next':5,'/today':None,'/week':None,'/all':None}
            heading={'/next':'📍 <b>Eng yaqin deadline’lar</b>', '/today':'🕐 <b>Keyingi 24 soat</b>',
                     '/week':'📅 <b>Keyingi 7 kun</b>', '/all':'📋 <b>Barcha kelgusi deadline’lar</b>'}[command]
            self.send_events(cid,heading,events,now,caps[command])
        elif command=='/track':
            parts=args.split()
            if len(parts)!=2 or parts[1].lower() not in ('on','off') or not SUPPORTED_TRACK.fullmatch(parts[0]):
                self.send(cid,'Format: <code>/track eng_ui on</code> yoki <code>/track eng_ui off</code>');return
            tr,st=parts[0].lower(),parts[1].lower()
            if tr=='common':
                self.send(cid,'Umumiy fanlar doim yoqilgan; bu track o‘chirilmaydi.');return
            self.store.track_change(cid,tr,st=='on')
            self.send(cid,f'✅ {esc(label_track(tr))}: <b>{"yoqildi" if st=="on" else "o‘chirildi"}</b>.\n'
                          'Boshqa levelda ma’lumot mavjud bo‘lmasa, admin uni /add bilan kiritishi kerak.')
        elif command=='/status':
            status='yoqilgan ✅' if self.store.enabled(cid) else 'o‘chirilgan 🔕'
            tracks=', '.join(esc(label_track(tr)) for tr in sorted(self.store.subscribed(cid)))
            events=self.store.list_events(cid,now=now)
            nxt=event_line(events[0],now,verbose=False) if events else 'Hozircha kelgusi deadline yo‘q.'
            self.send(cid,f'⚙️ <b>Holat:</b> {status}\n👥 <b>Fan/level:</b> {tracks}\n'
                          f'🗂 <b>Kelgusi vazifalar:</b> {len(events)}\n\n<b>Eng yaqin:</b>\n{nxt}')
        elif command=='/unknown':
            rows=[x for x in self.store.undated if x['track'] in self.store.subscribed(cid)]
            if not rows:self.send(cid,'Aniq deadline’i ko‘rsatilmagan qo‘shimcha vazifalar yo‘q.');return
            msg='❔ <b>Moodle’da deadline ko‘rsatilmagan:</b>\n\n'
            for x in rows:
                msg+=f'• {esc(x["subject"])} — {esc(x["topic"])}\n'
                if x.get('url'):msg+=f'  <a href="{esc(x["url"])}">Moodle’da tekshirish</a>\n'
            self.send(cid,msg)
        elif command=='/test':
            events=self.store.list_events(cid,now=now)[:1]
            self.send_events(cid,'🔔 <b>Sinov xabari (haqiqiy jadvaldan)</b>',events,now)
        elif command=='/add':
            if not args:
                self.send(cid,ADD_HELP);return
            vals=[x.strip() for x in args.split('|')]
            if len(vals)!=6:
                self.send(cid,ADD_HELP);return
            date,subject,nazorat,topic,track,url=vals
            track=track.lower()
            try:date=parse_user_date(date)
            except ValueError:self.send(cid,'❌ Sana formati: <code>2026-12-04 23:59</code>');return
            if parse_due(date)<=now:self.send(cid,'❌ O‘tib ketgan sanani kiritmang.');return
            if not SUPPORTED_TRACK.fullmatch(track) or not valid_url(url) or not all(vals[1:4]) or any(len(x)>200 for x in vals[1:4]):
                self.send(cid,'❌ Fan/mavzu/level/havola formati noto‘g‘ri. /add buyrug‘ini alohida yuborib namuna ko‘ring.');return
            code=self.store.add_event(cid,date,subject,nazorat,topic,track,url)
            self.send(cid,f'✅ Qo‘shildi: <code>{code}</code>\n{esc(subject)} · {esc(nazorat)}\n'
                          f'{esc(display_due(date))} · {esc(label_track(track))}\n'
                          'Eslatma: level common bo‘lmasa, kerakli /track kodini yoqing.')
        elif command=='/setdate':
            spl=args.split(maxsplit=1)
            if len(spl)!=2 or not self.store.has_event(cid,spl[0].upper()):
                self.send(cid,'Format: <code>/setdate U002 2026-11-15 23:00</code>');return
            ev_id=spl[0].upper()
            try:new_date=parse_user_date(spl[1])
            except ValueError:self.send(cid,'❌ Sana noto‘g‘ri. Masalan: 2026-12-05 23:59');return
            if parse_due(new_date)<=now:self.send(cid,'❌ O‘tgan sanaga o‘zgartirish mumkin emas.');return
            self.store.override(cid,ev_id,deadline=new_date)
            self.send(cid,f'✅ <code>{esc(ev_id)}</code> deadline’i faqat shu chat uchun <b>{esc(display_due(new_date))}</b> ga o‘zgartirildi.')
        elif command=='/topic':
            spl=args.split(maxsplit=1)
            if len(spl)!=2 or not self.store.has_event(cid,spl[0].upper()) or len(spl[1])>350:
                self.send(cid,'Format: <code>/topic U002 Yangi aniq mavzu</code>');return
            ev_id=spl[0].upper();self.store.override(cid,ev_id,topic=spl[1].strip())
            self.send(cid,f'✅ <code>{esc(ev_id)}</code> mavzusi shu chat uchun yangilandi.')
        elif command in ('/hide','/unhide'):
            ev_id=args.strip().upper()
            if not self.store.has_event(cid,ev_id):
                self.send(cid,'❌ Vazifa topilmadi. /all dan kodini ko‘ring.');return
            self.store.override(cid,ev_id,hidden=(command=='/hide'))
            self.send(cid,f'✅ <code>{esc(ev_id)}</code> {"yashirildi" if command=="/hide" else "qaytarildi"}.')
        elif command=='/remove':
            ev_id=args.strip().upper()
            if self.store.remove_event(cid,ev_id):
                self.send(cid,f'✅ <code>{esc(ev_id)}</code> o‘chirildi.')
            else:
                self.send(cid,'❌ Faqat shu chatda admin qo‘shgan vazifani /remove bilan o‘chirish mumkin. '
                              'Moodle’dan import qilingan vazifani /hide ID bilan yashiring.')
        else:
            self.send(cid,'Buyruq tushunarsiz. /help — imkoniyatlar ro‘yxati.')

    def dispatch_due(self, now: datetime | None = None) -> int:
        now=now or local_now()
        count=0
        for chat in self.store.known_chats():
            cid=int(chat['chat_id'])
            pending=pending_reminders(self.store,cid,now)
            for hours,evs in sorted(pending.items(),reverse=True):
                label=label_lead(hours)
                heading=f'⏰ <b>DEADLINE’GA {esc(label.upper())} QOLDI</b>\n📍 Toshkent vaqti (UTC+5)'
                chunks=take_chunks([notification_line(ev,now) for ev in evs],heading)
                try:
                    for chunk in chunks:
                        self.send(cid,chunk)
                    self.store.mark_sent(cid,evs,hours,now)
                    count+=len(evs)
                except Exception as exc:
                    LOGGER.exception('Could not notify chat %s: %s',cid,exc)
        return count

    def serve(self):
        offset=self.store.get_offset()
        last_scan=0.0
        LOGGER.info('Polling started; %d confirmed deadlines',len(self.store.seed))
        while True:
            try:
                if time.monotonic()-last_scan >= 30:
                    self.dispatch_due()
                    last_scan=time.monotonic()
                updates=self.api.call('getUpdates',offset=offset,timeout=20,allowed_updates=['message'])
                for update in updates:
                    try:
                        if 'message' in update:self.handle_message(update['message'])
                    except Exception:
                        LOGGER.exception('Error processing update %s',update.get('update_id'))
                    offset=max(offset,update['update_id']+1)
                    self.store.set_offset(offset)
            except (HTTPError,URLError,TimeoutError,OSError,RuntimeError,ValueError) as exc:
                LOGGER.warning('Polling error: %s; retry after 8 sec',exc)
                time.sleep(8)
            except KeyboardInterrupt:
                LOGGER.info('Stopped')
                return


def main():
    load_dotenv(ROOT/'.env')
    token=os.environ.get('BOT_TOKEN','').strip()
    if not token or ':' not in token:
        print('BOT_TOKEN topilmadi. .env.example faylidan .env yarating va BotFather tokenini kiriting.',file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    store=Store(os.environ.get('DB_PATH',str(ROOT/'data'/'uwed_bot.sqlite3')))
    bot=Bot(TelegramAPI(token),store)
    try:
        me=bot.api.call('getMe')
        LOGGER.info('Telegram bot connected: @%s',me.get('username'))
        try:
            bot.api.call('setMyCommands',commands=[
                {'command':'start','description':'Botni ishga tushirish'},
                {'command':'next','description':'Eng yaqin 5 ta deadline'},
                {'command':'today','description':'24 soat ichidagi deadline'},
                {'command':'week','description':'7 kunlik deadline'},
                {'command':'all','description':'Barcha deadline lar'},
                {'command':'status','description':'Eslatmalar va level holati'},
                {'command':'track','description':'Til levelini tanlash'},
                {'command':'enable','description':'Guruhda eslatmani yoqish (admin)'},
                {'command':'help','description':'Bot buyruqlari'}])
        except Exception as exc:
            LOGGER.warning('Telegram commands menyusini sozlab bo‘lmadi: %s',exc)
    except Exception as exc:
        LOGGER.error('BotFather token xato yoki internet mavjud emas: %s',exc)
        return 1
    bot.serve()
    return 0

if __name__=='__main__':
    sys.exit(main())
