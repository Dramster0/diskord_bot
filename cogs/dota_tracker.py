import os
import json
import re
import asyncio
import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from bs4 import BeautifulSoup
import aiohttp

# ===================== НАСТРОЙКИ =====================

# Канал, куда постить результаты — из .env
RESULTS_CHANNEL_ID = int(os.getenv("DOTA_RESULTS_CHANNEL_ID", "0"))

# Стартовая страница турнира (используется только при самом первом запуске,
# пока никто ни разу не вызвал /турнир — дальше текущий турнир хранится
# в CONFIG_FILE и переключается командой).
DEFAULT_TOURNAMENT_PAGE = os.getenv(
    "DOTA_TOURNAMENT_PAGE", "Esports_World_Cup/2026/Group_Stage"
)

TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
TZ = ZoneInfo(TIMEZONE)

# Как часто проверять страницу (в минутах). Правила Liquipedia требуют не чаще
# 1 запроса в 30 секунд для action=parse — 10 минут даёт огромный запас.
POLL_INTERVAL_MINUTES = int(os.getenv("DOTA_POLL_INTERVAL_MINUTES", "10"))

API_URL = "https://liquipedia.net/dota2/api.php"

# Общая мета героев по текущему патчу (не привязана к конкретному турниру) —
# чистый JSON API, разрешён их robots.txt (только Crawl-delay: 2 между запросами)
D2PT_API_URL = "https://dota2protracker.com/api/heroes/list"
D2PT_POSITION_NAMES = {
    "1": "Carry (1)",
    "2": "Mid (2)",
    "3": "Offlane (3)",
    "4": "Support (4)",
    "5": "Hard Support (5)",
}

# Обязательно по правилам API Liquipedia — свой понятный User-Agent.
# Впиши что-то реальное, если будешь спрашивать поддержку Liquipedia про доступ.
HEADERS = {
    "User-Agent": "PersonalDiscordBot/1.0 (personal non-commercial use)"
}

# esport.vision — трекер live-матчей с реальными пиками/банами героев.
# Прямого поиска по названию команды у них нет, поэтому используем открытый
# JSON-фид со списком live-матчей (/matches) и находим нужный по именам команд,
# а сами пики/баны берём со второго открытого эндпоинта (/stats/{id}).
ESV_MATCHES_URL = "https://esport.vision/matches"
ESV_STATS_URL = "https://esport.vision/stats"

# Канал для напоминаний о скором начале матча. Если не задан отдельно —
# используется тот же канал, что и для результатов.
_reminder_channel_raw = os.getenv("DOTA_REMINDER_CHANNEL_ID", "")
REMINDER_CHANNEL_ID = int(_reminder_channel_raw) if _reminder_channel_raw else RESULTS_CHANNEL_ID

# За сколько минут до начала матча слать напоминание.
REMINDER_MINUTES = int(os.getenv("DOTA_REMINDER_MINUTES", "15"))

_BASE_DIR = os.path.join(os.path.dirname(__file__), "..")


def normalize_page(page: str) -> str:
    """Приводит название страницы турнира к единому виду (подчёркивания вместо
    пробелов) — MediaWiki-поиск возвращает названия с пробелами, а конфиг и
    прямые ссылки обычно используют подчёркивания. Без этого один и тот же
    турнир превращался бы в два разных ключа и терял историю."""
    return page.strip().replace(" ", "_")

# Файл, где храним, какие матчи уже были опубликованы — чтобы не дублировать
# при перезапуске бота.
SEEN_MATCHES_FILE = os.path.join(_BASE_DIR, "dota_seen_matches.json")
# Файл, где храним, о каких матчах уже успели напомнить — чтобы не слать
# одно и то же напоминание дважды.
REMINDED_MATCHES_FILE = os.path.join(_BASE_DIR, "dota_reminded_matches.json")
STARTED_MATCHES_FILE = os.path.join(_BASE_DIR, "dota_started_matches.json")
# Файл, где храним, какой турнир сейчас отслеживается.
CONFIG_FILE = os.path.join(_BASE_DIR, "dota_config.json")


class TournamentSelect(discord.ui.Select):
    def __init__(self, cog: "DotaTracker", results: list[dict]):
        options = [
            discord.SelectOption(label=r["title"][:100], value=r["title"])
            for r in results
        ]
        super().__init__(placeholder="Выбери турнир...", options=options)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        chosen_page = self.values[0]
        await interaction.response.edit_message(
            content=f"Переключаюсь на **{chosen_page.replace('_', ' ')}**, секунду...",
            view=None,
        )
        await self.cog.switch_tournament(chosen_page)
        await interaction.edit_original_response(
            content=(
                f"Готово! Теперь слежу за турниром: **{chosen_page.replace('_', ' ')}**\n"
                f"История результатов каждого турнира хранится отдельно — "
                f"можно свободно переключаться между турнирами и возвращаться обратно."
            )
        )


class TournamentSelectView(discord.ui.View):
    def __init__(self, cog: "DotaTracker", results: list[dict]):
        super().__init__(timeout=60)
        self.add_item(TournamentSelect(cog, results))


class DotaTracker(commands.Cog):
    """Слежение за результатами турнира Liquipedia и команда /расписание."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.current_page = self._load_current_page()
        # Теперь это словарь {страница_турнира: множество ключей матчей} —
        # у каждого турнира своя отдельная история, при переключении ничего не теряется.
        self.seen_matches_by_page: dict[str, set[str]] = self._load_seen()
        self.reminded_matches_by_page: dict[str, set[str]] = self._load_reminded()
        self.started_matches_by_page: dict[str, set[str]] = self._load_started()
        self._html_cache: str | None = None
        self._html_cache_time: float = 0
        self._scheduled_keys: set[str] = set()
        self._d2pt_cache: list[dict] | None = None
        self._d2pt_cache_time: float = 0
        self._esv_matches_cache: list[dict] | None = None
        self._esv_matches_cache_time: float = 0

        loaded_count = len(self.seen_matches_by_page.get(self.current_page, set()))
        print(
            f"[dota_tracker] Загружен турнир: {self.current_page} "
            f"(уже опубликованных матчей в истории: {loaded_count})"
        )

        self.poll_task.change_interval(minutes=POLL_INTERVAL_MINUTES)
        self.poll_task.start()

    def cog_unload(self):
        self.poll_task.cancel()

    @property
    def seen_matches(self) -> set[str]:
        """Множество уже опубликованных матчей для текущего турнира."""
        return self.seen_matches_by_page.setdefault(self.current_page, set())

    @property
    def reminded_matches(self) -> set[str]:
        """Множество матчей, о которых уже отправили напоминание, для текущего турнира."""
        return self.reminded_matches_by_page.setdefault(self.current_page, set())

    @property
    def started_matches(self) -> set[str]:
        """Множество матчей, о начале которых уже объявили, для текущего турнира."""
        return self.started_matches_by_page.setdefault(self.current_page, set())

    # ---------- Какой турнир сейчас отслеживаем ----------

    def _load_current_page(self) -> str:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return normalize_page(json.load(f).get("page", DEFAULT_TOURNAMENT_PAGE))
        except (FileNotFoundError, json.JSONDecodeError):
            return normalize_page(DEFAULT_TOURNAMENT_PAGE)

    def _save_current_page(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"page": self.current_page}, f, ensure_ascii=False)

    async def switch_tournament(self, page: str):
        """Переключает слежение на другой турнир. История результатов у каждого
        турнира своя и сохраняется — можно свободно переключаться туда-обратно.
        Если турнир открывается впервые, все уже сыгранные на нём матчи сразу
        помечаются как «уже видели», чтобы бот не зафлудил канал старой историей."""
        page = normalize_page(page)
        is_first_time = page not in self.seen_matches_by_page

        self.current_page = page
        self._save_current_page()
        self._html_cache = None
        self._html_cache_time = 0

        if is_first_time:
            try:
                html = await self.fetch_page_html()
                loop = asyncio.get_event_loop()
                matches = await loop.run_in_executor(None, self._parse_matches, html)
                self.seen_matches_by_page[page] = {
                    m["key"] for m in matches if m["finished"]
                }
                self._save_seen()
            except Exception as e:
                print(f"[dota_tracker] Не удалось предзагрузить историю турнира {page}: {e}")
                self.seen_matches_by_page[page] = set()

    # ---------- Хранение уже опубликованных матчей (по турнирам) ----------

    def _load_seen(self) -> dict[str, set[str]]:
        try:
            with open(SEEN_MATCHES_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            # поддержка старого формата (просто список) — на случай апгрейда
            if isinstance(raw, list):
                return {DEFAULT_TOURNAMENT_PAGE: set(raw)}
            # нормализуем ключи и объединяем дубли (пробелы/подчёркивания
            # раньше давали два разных ключа для одного и того же турнира)
            merged: dict[str, set[str]] = {}
            for page, keys in raw.items():
                norm_page = normalize_page(page)
                merged.setdefault(norm_page, set()).update(keys)
            return merged
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_seen(self):
        with open(SEEN_MATCHES_FILE, "w", encoding="utf-8") as f:
            serializable = {page: list(keys) for page, keys in self.seen_matches_by_page.items()}
            json.dump(serializable, f, ensure_ascii=False)

    def _load_reminded(self) -> dict[str, set[str]]:
        try:
            with open(REMINDED_MATCHES_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            merged: dict[str, set[str]] = {}
            for page, keys in raw.items():
                norm_page = normalize_page(page)
                merged.setdefault(norm_page, set()).update(keys)
            return merged
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_reminded(self):
        with open(REMINDED_MATCHES_FILE, "w", encoding="utf-8") as f:
            serializable = {page: list(keys) for page, keys in self.reminded_matches_by_page.items()}
            json.dump(serializable, f, ensure_ascii=False)

    def _load_started(self) -> dict[str, set[str]]:
        try:
            with open(STARTED_MATCHES_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            merged: dict[str, set[str]] = {}
            for page, keys in raw.items():
                norm_page = normalize_page(page)
                merged.setdefault(norm_page, set()).update(keys)
            return merged
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_started(self):
        with open(STARTED_MATCHES_FILE, "w", encoding="utf-8") as f:
            serializable = {page: list(keys) for page, keys in self.started_matches_by_page.items()}
            json.dump(serializable, f, ensure_ascii=False)

    # ---------- Получение и разбор страницы ----------

    async def fetch_page_html(self) -> str:
        """Скачивает сырой HTML турнирной страницы через официальный API,
        с коротким кэшем — правила Liquipedia просят не чаще 1 запроса
        в 30 секунд для action=parse."""
        now = asyncio.get_event_loop().time()
        if self._html_cache and (now - self._html_cache_time) < 30:
            return self._html_cache

        params = {
            "action": "parse",
            "page": self.current_page,
            "format": "json",
            "prop": "text",
        }
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(API_URL, params=params, timeout=30) as resp:
                resp.raise_for_status()
                data = await resp.json()

        html = data["parse"]["text"]["*"]
        self._html_cache = html
        self._html_cache_time = now
        return html

    async def fetch_patch_meta(self) -> list[dict]:
        """Общая статистика героев по текущему патчу с Dota2ProTracker
        (не привязана к конкретному турниру). Кэш на 15 минут — их
        robots.txt просит Crawl-delay: 2, но данные и так меняются не часто."""
        now = asyncio.get_event_loop().time()
        if self._d2pt_cache and (now - self._d2pt_cache_time) < 900:
            return self._d2pt_cache

        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(D2PT_API_URL, timeout=20) as resp:
                resp.raise_for_status()
                data = await resp.json()

        self._d2pt_cache = data
        self._d2pt_cache_time = now
        return data

    # ---------- esport.vision: live-матч по названиям команд + реальные пики ----------

    async def fetch_esv_matches(self) -> list[dict]:
        """Список live/недавних матчей с esport.vision — открытый JSON-фид,
        тот же самый, что подгружает их собственная главная страница.
        Короткий кэш (15 сек): список live-матчей быстро меняется, а
        /прогноз не должен дёргать сайт лишний раз при повторных вызовах."""
        now = asyncio.get_event_loop().time()
        if self._esv_matches_cache is not None and (now - self._esv_matches_cache_time) < 15:
            return self._esv_matches_cache

        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(ESV_MATCHES_URL, timeout=15) as resp:
                resp.raise_for_status()
                data = await resp.json()

        self._esv_matches_cache = data
        self._esv_matches_cache_time = now
        return data

    async def fetch_esv_stats(self, match_id) -> dict:
        """Полные данные конкретного матча с esport.vision: пики, баны и
        счёт по картам. Это тот же эндпоинт, что и страница match.html?id=...
        дёргает у себя же (/stats/{id})."""
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(f"{ESV_STATS_URL}/{match_id}", timeout=15) as resp:
                resp.raise_for_status()
                return await resp.json()

    @staticmethod
    def _split_esv_teams(teams_str: str) -> tuple[str, str]:
        """esport.vision отдаёт команды одной строкой 'Team1 vs Team2'."""
        parts = [p.strip() for p in teams_str.split(" vs ")]
        if len(parts) != 2:
            return "", ""
        return parts[0], parts[1]

    @staticmethod
    def _fuzzy_name_match(a: str, b: str) -> bool:
        """Мягкое сравнение названий команд: названия на esport.vision и на
        Liquipedia не всегда совпадают дословно (сокращения, регистр), но
        одно почти всегда является подстрокой другого."""
        a, b = a.strip().lower(), b.strip().lower()
        if not a or not b:
            return False
        return a in b or b in a

    async def find_esv_live_match(self, team1_name: str, team2_name: str) -> dict | None:
        """Ищет live-матч этих двух команд на esport.vision — по названиям,
        без ручного ввода id или ссылок. Возвращает None, если такого
        live-матча сейчас нет (например, играют не прямо сейчас)."""
        try:
            esv_matches = await self.fetch_esv_matches()
        except Exception as e:
            print(f"[dota_tracker] esport.vision /matches недоступен: {e}")
            return None

        for m in esv_matches:
            if m.get("finished"):
                continue
            esv_t1, esv_t2 = self._split_esv_teams(m.get("teams", ""))
            if not esv_t1 or not esv_t2:
                continue

            direct = self._fuzzy_name_match(team1_name, esv_t1) and self._fuzzy_name_match(team2_name, esv_t2)
            swapped = self._fuzzy_name_match(team1_name, esv_t2) and self._fuzzy_name_match(team2_name, esv_t1)
            if direct or swapped:
                match = dict(m)
                match["_esv_team1"] = esv_t1
                match["_esv_team2"] = esv_t2
                # Нужно, чтобы позже сопоставить radiant/dire именно с нашими
                # team1/team2 (esport.vision может перечислить их в обратном
                # порядке относительно того, как их назвали в /прогноз).
                match["_swapped"] = swapped
                return match

        return None

    def _build_esv_draft_summary(
        self, stats: dict, esv_match: dict, team1_name: str, team2_name: str
    ) -> str | None:
        """Собирает текстовое описание текущих пиков/банов карты матча из
        ответа /stats/{id}. Возвращает None, если данных о драфте пока нет
        (например, драфт ещё не начался) или структура ответа неожиданная —
        esport.vision источник вспомогательный, поэтому здесь мы просто
        молча отступаем, а не роняем всю команду /прогноз."""
        try:
            all_matches = stats.get("allMatches") or []
            current_num = stats.get("currentMatchNumber", 1)
            current_map = next((mm for mm in all_matches if mm.get("number") == current_num), None)
            if current_map is None and all_matches:
                current_map = all_matches[-1]
            if current_map is None:
                return None

            is_t1_radiant = current_map.get("isTeam1Radiant")
            if is_t1_radiant is None:
                is_t1_radiant = stats.get("isTeam1Radiant")
            # Если так и не удалось узнать — считаем, что esv-team1 играет
            # радиантом, это просто дефолт на случай отсутствующих данных.
            esv_team1_is_radiant = bool(is_t1_radiant) if is_t1_radiant is not None else True
            swapped = esv_match.get("_swapped", False)

            def esv_side_to_our_team(is_radiant_side: bool) -> str:
                is_esv_team1 = (is_radiant_side == esv_team1_is_radiant)
                if not swapped:
                    return team1_name if is_esv_team1 else team2_name
                return team2_name if is_esv_team1 else team1_name

            team_for_radiant = esv_side_to_our_team(True)
            team_for_dire = esv_side_to_our_team(False)

            picks = current_map.get("picks") or {}
            radiant_picks = [p.get("heroName") for p in (picks.get("radiant") or []) if p.get("heroName")]
            dire_picks = [p.get("heroName") for p in (picks.get("dire") or []) if p.get("heroName")]

            lines = []
            if radiant_picks:
                lines.append(f"{team_for_radiant}: {', '.join(radiant_picks)}")
            if dire_picks:
                lines.append(f"{team_for_dire}: {', '.join(dire_picks)}")

            # Если финальных пиков ещё нет (драфт в процессе) — берём
            # черновой фид, там же видно и баны. match.draft — основной
            # источник, liveDraft — резервный (см. разбор match.html).
            if not lines:
                draft = current_map.get("draft") or []
                live_draft = stats.get("liveDraft") or {}
                if (
                    not draft
                    and live_draft.get("draft")
                    and live_draft.get("mapNumber") == current_num
                ):
                    draft = live_draft["draft"]
                    ld_t1_radiant = live_draft.get("isTeam1Radiant")
                    if ld_t1_radiant is not None:
                        esv_team1_is_radiant = bool(ld_t1_radiant)
                        team_for_radiant = esv_side_to_our_team(True)
                        team_for_dire = esv_side_to_our_team(False)

                picks_r, picks_d, bans_r, bans_d = [], [], [], []
                for d in draft:
                    name = d.get("heroName") or d.get("hero")
                    if not name:
                        continue
                    side_bucket = picks_r if d.get("side") == "radiant" else picks_d
                    ban_bucket = bans_r if d.get("side") == "radiant" else bans_d
                    (side_bucket if d.get("action") == "pick" else ban_bucket).append(name)

                if picks_r:
                    lines.append(f"{team_for_radiant} уже пикнули: {', '.join(picks_r)}")
                if picks_d:
                    lines.append(f"{team_for_dire} уже пикнули: {', '.join(picks_d)}")
                if bans_r:
                    lines.append(f"{team_for_radiant} забанили: {', '.join(bans_r)}")
                if bans_d:
                    lines.append(f"{team_for_dire} забанили: {', '.join(bans_d)}")

            if not lines:
                return None

            return (
                f"Реальный live-драфт этого матча прямо сейчас (карта {current_num}, "
                f"источник esport.vision): " + "; ".join(lines)
            )
        except Exception as e:
            print(f"[dota_tracker] Не удалось разобрать пики esport.vision: {e}")
            return None

    async def fetch_esv_draft_context(self, team1_name: str, team2_name: str) -> str | None:
        """Полный пайплайн для /прогноз: найти live-матч этих команд на
        esport.vision (без ручного ввода id) и собрать текст с реальными
        пиками/банами для промпта ИИ. Возвращает None на любой осечке —
        esport.vision тут вспомогательный источник, а не критичный: /прогноз
        должен продолжать работать и без него."""
        esv_match = await self.find_esv_live_match(team1_name, team2_name)
        if esv_match is None:
            return None

        try:
            stats = await self.fetch_esv_stats(esv_match["id"])
        except Exception as e:
            print(f"[dota_tracker] esport.vision /stats/{esv_match['id']} недоступен: {e}")
            return None

        return self._build_esv_draft_summary(stats, esv_match, team1_name, team2_name)

    async def fetch_matches(self) -> list[dict]:
        """Скачивает страницу турнира через официальный API и парсит матчи.
        Комбинирует два формата разметки Liquipedia: таблицы группового этапа
        (brkts-matchlist) и таблицы расписания стадий вроде Survival/Playoffs
        (table2__table) — так поддерживаются оба вида страниц турнира."""
        html = await self.fetch_page_html()
        loop = asyncio.get_event_loop()
        matches = await loop.run_in_executor(None, self._parse_matches, html)
        matches += await loop.run_in_executor(None, self._parse_schedule_tables, html)
        return matches

    async def search_tournaments(self, query: str) -> list[dict]:
        """Ищет страницы турниров на Liquipedia по названию через MediaWiki search API."""
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": 5,
            "format": "json",
            "utf8": 1,
        }
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(API_URL, params=params, timeout=15) as resp:
                resp.raise_for_status()
                data = await resp.json()

        results = data.get("query", {}).get("search", [])
        return [{"title": r["title"]} for r in results]

    def _parse_standings(self, html: str, group_letter: str) -> list[dict] | None:
        """Парсит таблицу 'Standings' для конкретной группы (A, B, C, D...)."""
        soup = BeautifulSoup(html, "html.parser")
        heading = soup.find("h3", id=f"Group_{group_letter.upper()}")
        if heading is None:
            return None

        heading_div = heading.parent
        table_div = heading_div.find_next_sibling("div")
        if table_div is None:
            return None
        table = table_div.find("table", class_="grouptable")
        if table is None:
            return None

        rows = table.find_all("tr")[1:]  # первая строка — просто заголовок "Standings"
        standings = []
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            position = cells[0].get_text(strip=True)
            team_span = cells[1].find("span", class_="team-template-text")
            team = team_span.get_text(strip=True) if team_span else cells[1].get_text(strip=True)
            record = cells[2].get_text(strip=True)
            score = cells[3].get_text(strip=True)
            standings.append({
                "position": position,
                "team": team,
                "record": record,
                "score": score,
            })
        return standings

    def _parse_hero_stats(self, html: str) -> list[dict]:
        """Парсит таблицу 'Hero Statistics' — винрейты героев на турнире."""
        soup = BeautifulSoup(html, "html.parser")
        heading = soup.find(id="Hero_Statistics")
        if heading is None:
            return []

        heading_div = heading.parent
        table_container = heading_div.find_next_sibling("div")
        if table_container is None:
            return []
        table = table_container.find("table")
        if table is None:
            return []

        rows = table.find("tbody").find_all("tr", class_="character-stats-row")
        heroes = []
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 19:
                continue

            hero_links = cells[1].find_all("a")
            hero_name = hero_links[-1].get_text(strip=True) if hero_links else cells[1].get_text(strip=True)

            heroes.append({
                "hero": hero_name,
                "picks": cells[2].get_text(strip=True),
                "wins": cells[3].get_text(strip=True),
                "losses": cells[4].get_text(strip=True),
                "winrate": cells[5].get_text(strip=True),
                "pick_pct": cells[6].get_text(strip=True),
                "radiant_wr": cells[10].get_text(strip=True),
                "dire_wr": cells[14].get_text(strip=True),
                "bans": cells[15].get_text(strip=True),
                "picks_bans_pct": cells[18].get_text(strip=True),
            })
        return heroes

    def _parse_streams(self, html: str) -> list[dict]:
        """Парсит раздел 'Streams' и достаёт ссылки для русскоязычных трансляций."""
        soup = BeautifulSoup(html, "html.parser")
        heading = soup.find(id="Streams")
        if heading is None:
            return []

        container = heading.parent.find_next_sibling("div")
        if container is None:
            return []

        results = []
        for table in container.find_all("table"):
            section_th = table.find("th", colspan="100")
            section_name = section_th.get_text(strip=True) if section_th else "Streams"

            rows = table.find_all("tr")
            language_row = None
            streams_row = None
            for row in rows:
                header = row.find("th")
                if header is None:
                    continue
                if header.get_text(strip=True) == "Language":
                    language_row = row
                elif header.get_text(strip=True) == "Streams":
                    streams_row = row

            if language_row is None or streams_row is None:
                continue

            lang_cells = language_row.find_all("td")
            stream_cells = streams_row.find_all("td")

            for lang_cell, stream_cell in zip(lang_cells, stream_cells):
                img = lang_cell.find("img")
                lang_name = img.get("alt", "") if img else ""
                if "russian" not in lang_name.lower():
                    continue

                links = [a.get("href") for a in stream_cell.find_all("a") if a.get("href")]
                if links:
                    results.append({"section": section_name, "links": links})

        return results

    def _parse_matches(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        matches = []
        current_group = None

        for node in soup.descendants:
            if not hasattr(node, "get"):
                continue

            # Заголовки групп перед таблицами МАТЧЕЙ: <h6 id="Group_A_2">Group A</h6>
            # (не путать с <h3 id="Group_A"> — это заголовок таблицы standings,
            # все четыре таких h3 идут подряд в начале страницы и для меток
            # конкретных матчей не годятся)
            if node.name == "h6":
                node_id = node.get("id", "")
                if node_id.startswith("Group_"):
                    current_group = node.get_text(strip=True)
                continue

            if node.name != "div":
                continue
            classes = node.get("class") or []
            if "brkts-matchlist-match" not in classes:
                continue

            match = self._parse_single_match(node, current_group)
            if match:
                matches.append(match)

        return matches

    def _parse_single_match(self, match_div, group_name):
        opponents = match_div.find_all(
            "div", class_="brkts-matchlist-opponent", recursive=False
        )
        scores = match_div.find_all(
            "div", class_="brkts-matchlist-score", recursive=False
        )
        if len(opponents) != 2:
            return None

        team1 = opponents[0].get("aria-label", "TBD").strip()
        team2 = opponents[1].get("aria-label", "TBD").strip()

        def short_name(opponent_cell):
            dyn = opponent_cell.find("div", class_="team-name-dynamic")
            return dyn.get("data-team-shortname", "").strip() if dyn else ""

        team1_short = short_name(opponents[0])
        team2_short = short_name(opponents[1])

        def score_text(cell):
            if cell is None:
                return ""
            content = cell.find("div", class_="brkts-matchlist-cell-content")
            return content.get_text(strip=True) if content else ""

        score1 = score_text(scores[0]) if len(scores) > 0 else ""
        score2 = score_text(scores[1]) if len(scores) > 1 else ""

        timer = match_div.find("span", class_="timer-object")
        timestamp = None
        finished = False
        if timer:
            ts = timer.get("data-timestamp")
            if ts:
                timestamp = int(ts)
            finished = timer.get("data-finished") == "finished"

        # Уникальный ключ матча — команды + время начала, этого достаточно,
        # чтобы отличать разные матчи между теми же командами в разных раундах.
        match_key = f"{team1}|{team2}|{timestamp}"

        return {
            "key": match_key,
            "group": group_name,
            "team1": team1,
            "team2": team2,
            "team1_short": team1_short,
            "team2_short": team2_short,
            "score1": score1,
            "score2": score2,
            "finished": finished,
            "live": False,  # формат группового этапа не даёт отдельного live-статуса
            "timestamp": timestamp,
        }

    def _parse_schedule_tables(self, html: str) -> list[dict]:
        """Парсит таблицы расписания стадий вроде Survival/Playoffs (формат
        table2__table) — используется на страницах, отличных от группового
        этапа. Работает для любого заголовка <h2>, под которым есть такая
        таблица, так что подхватит и будущие стадии (например, Playoffs),
        когда на Liquipedia появится соответствующий раздел."""
        soup = BeautifulSoup(html, "html.parser")
        matches = []

        for heading in soup.find_all("h2"):
            stage_name = heading.get_text(strip=True)
            container = heading.parent.find_next_sibling("div")
            if container is None:
                continue
            table = container.find("table", class_="table2__table")
            if table is None:
                continue

            rows = table.find_all("tr", class_="table2__row--body")
            for row in rows:
                match = self._parse_schedule_row(row, stage_name)
                if match:
                    matches.append(match)

        return matches

    def _parse_schedule_row(self, row, stage_name: str):
        cells = row.find_all("td")
        if len(cells) < 5:
            return None

        def team_names(cell):
            team_div = cell.find("div", class_="block-team")
            if team_div is None:
                return "TBD", ""
            full_el = team_div.find("span", class_="name hidden-xs")
            short_el = team_div.find("span", class_="name visible-xs")
            full = full_el.get_text(strip=True) if full_el else cell.get_text(strip=True)
            short = short_el.get_text(strip=True) if short_el else ""
            return full or "TBD", short

        team1, team1_short = team_names(cells[2])
        team2, team2_short = team_names(cells[4])

        # Отсеиваем «пустые» строки без единой известной команды — так в
        # парсер иногда может попасть посторонняя таблица вроде Prize Pool,
        # у которой просто нет секции block-team в нужных ячейках.
        if team1 == "TBD" and team2 == "TBD":
            return None

        timer = cells[0].find("span", class_="timer-object")
        timestamp = None
        if timer:
            ts = timer.get("data-timestamp")
            if ts:
                timestamp = int(ts)

        # В средней колонке три возможных состояния:
        #  - буквально текст "vs" — матч ещё не начался
        #  - цифры вроде "0:0" или "1:0" — серия идёт, но ещё не набрано
        #    нужное число побед
        #  - одна из сторон набрала нужное число побед по формату Bo3/Bo5 — серия завершена
        # HTML для "идёт" и "завершено" выглядит ОДИНАКОВО (просто цифры),
        # никакого специального признака нет — поэтому статус считаем по
        # формату серии (Best of N), а не по виду разметки.
        middle_cell = cells[3]
        middle_text = middle_cell.get_text(" ", strip=True)

        bo_format = None
        abbr = middle_cell.find("abbr")
        if abbr:
            bo_match = re.search(r"Best of (\d+)", abbr.get("title", ""))
            if bo_match:
                bo_format = int(bo_match.group(1))

        score1, score2 = "", ""
        finished = False
        live = False
        digits = re.findall(r"\d+", middle_text)
        if len(digits) >= 2 and "vs" not in middle_text.lower():
            score1, score2 = digits[0], digits[1]
            wins_needed = (bo_format // 2 + 1) if bo_format else None
            if wins_needed and max(int(score1), int(score2)) >= wins_needed:
                finished = True
            else:
                live = True

        match_key = f"{team1}|{team2}|{timestamp}"

        return {
            "key": match_key,
            "group": stage_name,
            "team1": team1,
            "team2": team2,
            "team1_short": team1_short,
            "team2_short": team2_short,
            "score1": score1,
            "score2": score2,
            "finished": finished,
            "live": live,
            "timestamp": timestamp,
        }

    # ---------- Фоновая проверка новых результатов ----------

    @tasks.loop(minutes=10)
    async def poll_task(self):
        try:
            matches = await self.fetch_matches()
        except Exception as e:
            print(f"[dota_tracker] Ошибка получения данных с Liquipedia: {e}")
            return

        await self._post_new_results(matches)
        await self._post_match_started(matches)
        await self._post_reminders(matches)

    async def _post_match_started(self, matches: list[dict]):
        """Объявляет о начале матча (когда счёт стал 'живым', но ещё не финальным).
        Если матч был обнаружен уже полностью завершённым (например, бот не успел
        поймать live-стадию между проверками) — отдельное объявление о старте
        не шлём, обычное объявление результата достаточно."""
        if RESULTS_CHANNEL_ID == 0:
            return

        channel = self.bot.get_channel(RESULTS_CHANNEL_ID)
        if channel is None:
            return

        newly_started = []
        for m in matches:
            if m.get("live") and m["key"] not in self.started_matches:
                newly_started.append(m)
                self.started_matches.add(m["key"])
            elif m["finished"]:
                # если матч уже сразу пойман завершённым, не нужно потом
                # ложно объявлять его "начавшимся" задним числом
                self.started_matches.add(m["key"])

        if newly_started:
            self._save_started()

        for m in newly_started:
            tournament_name = self.current_page.replace("_", " ")
            text = (
                f"🟢 **{m['group']}** — матч начался\n"
                f"**{m['team1']}** vs **{m['team2']}**\n"
                f"-# Турнир: {tournament_name}"
            )
            await channel.send(text)
            print(f"[dota_tracker] Объявлено начало матча: {m['team1']} vs {m['team2']}")

    async def _post_new_results(self, matches: list[dict]):
        if RESULTS_CHANNEL_ID == 0:
            print("[dota_tracker] DOTA_RESULTS_CHANNEL_ID не задан в .env")
            return

        channel = self.bot.get_channel(RESULTS_CHANNEL_ID)
        if channel is None:
            print(f"[dota_tracker] Канал с ID {RESULTS_CHANNEL_ID} не найден")
            return

        new_results = []
        already_finished_count = 0
        for m in matches:
            if not m["finished"]:
                continue
            if m["key"] in self.seen_matches:
                already_finished_count += 1
                continue
            new_results.append(m)
            self.seen_matches.add(m["key"])

        print(
            f"[dota_tracker] Проверка результатов: завершённых матчей всего "
            f"{already_finished_count + len(new_results)}, уже публиковалось "
            f"{already_finished_count}, новых {len(new_results)}"
        )

        if new_results:
            try:
                self._save_seen()
                print(f"[dota_tracker] История сохранена в {SEEN_MATCHES_FILE}")
            except Exception as e:
                print(f"[dota_tracker] ОШИБКА сохранения истории: {e}")

        for m in new_results:
            tournament_name = self.current_page.replace("_", " ")
            text = (
                f"🏆 **{m['group']}** — матч завершён\n"
                f"**{m['team1']}** {m['score1']} : {m['score2']} **{m['team2']}**\n"
                f"-# Источник: Liquipedia · Турнир: {tournament_name}"
            )
            await channel.send(text)
            print(f"[dota_tracker] Опубликован результат: {m['team1']} vs {m['team2']}")

    async def _post_reminders(self, matches: list[dict]):
        """Планирует точные напоминания на нужный момент времени. Сама проверка
        сайта нужна только чтобы узнать точное время начала матчей — дальше
        для каждого матча ставится отдельный таймер ровно на нужный момент,
        а не ожидание следующего цикла проверки."""
        if REMINDER_CHANNEL_ID == 0:
            return

        now_ts = datetime.datetime.now(tz=datetime.timezone.utc).timestamp()

        for m in matches:
            if m["finished"] or not m["timestamp"]:
                continue
            if m["key"] in self.reminded_matches:
                continue
            if m["key"] in self._scheduled_keys:
                continue  # уже поставлен точный таймер, ждём его срабатывания

            fire_at = m["timestamp"] - REMINDER_MINUTES * 60
            delay = fire_at - now_ts

            if m["timestamp"] <= now_ts:
                # матч уже должен был начаться, а мы его прозевали — напоминание не актуально
                self.reminded_matches.add(m["key"])
                self._save_reminded()
                continue

            self._scheduled_keys.add(m["key"])
            asyncio.create_task(self._send_reminder_after_delay(m, max(delay, 0)))
            print(
                f"[dota_tracker] Запланировано напоминание для "
                f"{m['team1']} vs {m['team2']} через {round(delay / 60)} мин."
            )

    async def _send_reminder_after_delay(self, m: dict, delay: float):
        await asyncio.sleep(delay)

        channel = self.bot.get_channel(REMINDER_CHANNEL_ID)
        if channel is None:
            print(f"[dota_tracker] Канал для напоминаний с ID {REMINDER_CHANNEL_ID} не найден")
            return

        now_ts = datetime.datetime.now(tz=datetime.timezone.utc).timestamp()
        actual_minutes_left = max(round((m["timestamp"] - now_ts) / 60), 0)

        tournament_name = self.current_page.replace("_", " ")
        text = (
            f"⏰ **{m['team1']}** vs **{m['team2']}** — "
            f"начало через {actual_minutes_left} мин.\n"
            f"-# Турнир: {tournament_name}"
        )
        await channel.send(text)
        self.reminded_matches.add(m["key"])
        self._save_reminded()
        self._scheduled_keys.discard(m["key"])
        print(f"[dota_tracker] Отправлено напоминание: {m['team1']} vs {m['team2']}")

    @poll_task.before_loop
    async def before_poll_task(self):
        await self.bot.wait_until_ready()

    # ---------- Команда переключения турнира ----------

    @discord.app_commands.command(
        name="турнир", description="Найти и переключить бота на другой турнир Dota 2"
    )
    @discord.app_commands.describe(название="Название турнира, например: Esports World Cup 2026")
    async def tournament(self, interaction: discord.Interaction, название: str):
        await interaction.response.defer()

        try:
            results = await self.search_tournaments(название)
        except Exception as e:
            await interaction.followup.send(f"Ошибка поиска на Liquipedia: {e}")
            return

        if not results:
            await interaction.followup.send(
                f"Ничего не нашлось по запросу «{название}». Попробуй сформулировать иначе."
            )
            return

        view = TournamentSelectView(self, results)
        await interaction.followup.send(
            "Нашёл несколько вариантов, выбери нужный турнир:", view=view
        )

    # ---------- Команда расписания ----------

    @discord.app_commands.command(
        name="расписание", description="Показать ближайшие матчи турнира"
    )
    async def schedule(self, interaction: discord.Interaction):
        await interaction.response.defer()

        try:
            matches = await self.fetch_matches()
        except Exception as e:
            await interaction.followup.send(f"Не удалось получить данные с Liquipedia: {e}")
            return

        upcoming = [m for m in matches if not m["finished"] and m["timestamp"]]
        upcoming.sort(key=lambda m: m["timestamp"])
        upcoming = upcoming[:10]

        if not upcoming:
            await interaction.followup.send("Ближайших матчей не найдено.")
            return

        lines = [f"**Ближайшие матчи** ({self.current_page.replace('_', ' ')}):"]
        for m in upcoming:
            dt = datetime.datetime.fromtimestamp(m["timestamp"], tz=TZ)
            time_str = dt.strftime("%d.%m %H:%M")
            lines.append(f"`{time_str}` [{m['group']}] **{m['team1']}** vs **{m['team2']}**")

        lines.append(f"-# Источник: Liquipedia · Турнир: {self.current_page.replace('_', ' ')}")
        await interaction.followup.send("\n".join(lines))

    # ---------- Команда расписания конкретной команды ----------

    @discord.app_commands.command(
        name="команда", description="Показать расписание конкретной команды (полное название или сокращение)"
    )
    @discord.app_commands.describe(
        название="Название команды или сокращение, например: Team Spirit или TSpirit"
    )
    async def team_schedule(self, interaction: discord.Interaction, название: str):
        await interaction.response.defer()

        try:
            matches = await self.fetch_matches()
        except Exception as e:
            await interaction.followup.send(f"Не удалось получить данные с Liquipedia: {e}")
            return

        query = название.strip().lower()

        def matches_query(m: dict) -> bool:
            candidates = (m["team1"], m["team2"], m["team1_short"], m["team2_short"])
            return any(c.lower() == query for c in candidates if c)

        team_matches = [m for m in matches if matches_query(m) and m["timestamp"]]

        if not team_matches:
            await interaction.followup.send(
                f"Не нашёл команду «{название}» на текущем турнире "
                f"({self.current_page.replace('_', ' ')}). Проверь написание или попробуй сокращение."
            )
            return

        team_matches.sort(key=lambda m: m["timestamp"])

        # Определяем отображаемое полное имя команды по первому найденному совпадению
        display_name = название
        first = team_matches[0]
        for full, short in ((first["team1"], first["team1_short"]), (first["team2"], first["team2_short"])):
            if full.lower() == query or short.lower() == query:
                display_name = full
                break

        lines = [f"**Расписание {display_name}**"]
        for m in team_matches:
            dt = datetime.datetime.fromtimestamp(m["timestamp"], tz=TZ)
            time_str = dt.strftime("%d.%m %H:%M")
            opponent = m["team2"] if m["team1"].lower() == display_name.lower() or m["team1_short"].lower() == query else m["team1"]

            if m["finished"]:
                lines.append(
                    f"`{time_str}` [{m['group']}] vs **{opponent}** — "
                    f"{m['score1']} : {m['score2']}"
                )
            else:
                lines.append(f"`{time_str}` [{m['group']}] vs **{opponent}** — предстоит")

        lines.append(f"-# Источник: Liquipedia · Турнир: {self.current_page.replace('_', ' ')}")
        await interaction.followup.send("\n".join(lines))


    # ---------- Команда турнирной таблицы группы ----------

    @discord.app_commands.command(
        name="группа", description="Показать турнирную таблицу группы (например A)"
    )
    @discord.app_commands.describe(буква="Буква группы, например: A")
    async def group_standings(self, interaction: discord.Interaction, буква: str):
        await interaction.response.defer()

        try:
            html = await self.fetch_page_html()
        except Exception as e:
            await interaction.followup.send(f"Не удалось получить данные с Liquipedia: {e}")
            return

        loop = asyncio.get_event_loop()
        standings = await loop.run_in_executor(None, self._parse_standings, html, буква)
        if standings is None:
            await interaction.followup.send(
                f"Группа «{буква}» не найдена на текущем турнире "
                f"({self.current_page.replace('_', ' ')})."
            )
            return

        lines = [f"**Группа {буква.upper()} — турнирная таблица**", "```"]
        lines.append(f"{'#':<3}{'Команда':<20}{'W-L-D':<8}{'Счёт':<6}")
        for s in standings:
            lines.append(
                f"{s['position']:<3}{s['team']:<20}{s['record']:<8}{s['score']:<6}"
            )
        lines.append("```")
        lines.append(f"-# Источник: Liquipedia · Турнир: {self.current_page.replace('_', ' ')}")
        await interaction.followup.send("\n".join(lines))

    # ---------- Команда подробных результатов группы ----------

    @discord.app_commands.command(
        name="ргруппы", description="Показать сыгранные матчи группы по дням (например A)"
    )
    @discord.app_commands.describe(буква="Буква группы, например: A")
    async def group_results(self, interaction: discord.Interaction, буква: str):
        await interaction.response.defer()

        try:
            html = await self.fetch_page_html()
        except Exception as e:
            await interaction.followup.send(f"Не удалось получить данные с Liquipedia: {e}")
            return

        loop = asyncio.get_event_loop()
        all_matches = await loop.run_in_executor(None, self._parse_matches, html)
        target_group = f"Group {буква.upper()}"
        group_matches = [
            m for m in all_matches
            if m["group"] == target_group and m["finished"] and m["timestamp"]
        ]

        if not group_matches:
            await interaction.followup.send(
                f"Сыгранных матчей в группе «{буква.upper()}» пока не найдено."
            )
            return

        group_matches.sort(key=lambda m: m["timestamp"])

        # группируем по дате (в выбранном часовом поясе)
        by_date: dict[str, list[dict]] = {}
        for m in group_matches:
            dt = datetime.datetime.fromtimestamp(m["timestamp"], tz=TZ)
            date_key = dt.strftime("%d.%m")
            by_date.setdefault(date_key, []).append(m)

        lines = [f"**Группа {буква.upper()} — результаты по дням**"]
        for date_key, day_matches in by_date.items():
            lines.append(f"\n__{date_key}__")
            for m in day_matches:
                lines.append(
                    f"**{m['team1']}** {m['score1']} : {m['score2']} **{m['team2']}**"
                )

        lines.append(f"\n-# Источник: Liquipedia · Турнир: {self.current_page.replace('_', ' ')}")
        await interaction.followup.send("\n".join(lines))

    # ---------- Команда всех результатов турнира разом ----------

    @discord.app_commands.command(
        name="результаты", description="Показать все сыгранные матчи турнира (по всем группам)"
    )
    async def all_results(self, interaction: discord.Interaction):
        await interaction.response.defer()

        try:
            html = await self.fetch_page_html()
        except Exception as e:
            await interaction.followup.send(f"Не удалось получить данные с Liquipedia: {e}")
            return

        loop = asyncio.get_event_loop()
        all_matches = await loop.run_in_executor(None, self._parse_matches, html)
        finished = [m for m in all_matches if m["finished"] and m["timestamp"]]

        if not finished:
            await interaction.followup.send("Сыгранных матчей пока не найдено.")
            return

        finished.sort(key=lambda m: (m["group"] or "", m["timestamp"]))

        # группируем по группе, внутри группы — по дате
        by_group: dict[str, dict[str, list[dict]]] = {}
        for m in finished:
            group_key = m["group"] or "Без группы"
            dt = datetime.datetime.fromtimestamp(m["timestamp"], tz=TZ)
            date_key = dt.strftime("%d.%m")
            by_group.setdefault(group_key, {}).setdefault(date_key, []).append(m)

        all_lines = [f"**Все результаты турнира** ({self.current_page.replace('_', ' ')})"]
        for group_key, dates in by_group.items():
            all_lines.append(f"\n__**{group_key}**__")
            for date_key, day_matches in dates.items():
                all_lines.append(f"_{date_key}_")
                for m in day_matches:
                    all_lines.append(
                        f"**{m['team1']}** {m['score1']} : {m['score2']} **{m['team2']}**"
                    )

        all_lines.append("\n-# Источник: Liquipedia")

        # Discord режет сообщения на 2000 символов — разбиваем на части по строкам
        chunks: list[str] = []
        current_chunk = ""
        for line in all_lines:
            candidate = f"{current_chunk}\n{line}" if current_chunk else line
            if len(candidate) > 1900:
                chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk = candidate
        if current_chunk:
            chunks.append(current_chunk)

        for chunk in chunks:
            await interaction.followup.send(chunk)

    # ---------- Команда винрейта героев ----------

    @discord.app_commands.command(
        name="wr", description="Показать статистику героев на текущем турнире"
    )
    @discord.app_commands.describe(количество="Сколько героев показать (по умолчанию 15)")
    async def hero_winrates(self, interaction: discord.Interaction, количество: int = 15):
        await interaction.response.defer()

        try:
            html = await self.fetch_page_html()
        except Exception as e:
            await interaction.followup.send(f"Не удалось получить данные с Liquipedia: {e}")
            return

        loop = asyncio.get_event_loop()
        heroes = await loop.run_in_executor(None, self._parse_hero_stats, html)
        if not heroes:
            await interaction.followup.send(
                "Статистика героев не найдена на текущем турнире."
            )
            return

        heroes = heroes[:количество]

        lines = [f"**Статистика героев** ({self.current_page.replace('_', ' ')})", "```"]
        header = f"{'Герой':<16}{'Picks':<7}{'W-L':<8}{'WR':<7}{'RadWR':<8}{'DireWR':<8}{'Bans':<6}"
        lines.append(header)
        for h in heroes:
            lines.append(
                f"{h['hero']:<16}{h['picks']:<7}{h['wins']+'-'+h['losses']:<8}"
                f"{h['winrate']:<7}{h['radiant_wr']:<8}{h['dire_wr']:<8}{h['bans']:<6}"
            )
        lines.append("```")
        lines.append(f"-# Источник: Liquipedia · Турнир: {self.current_page.replace('_', ' ')}")
        await interaction.followup.send("\n".join(lines))


    # ---------- Команда русскоязычных стримов ----------

    @discord.app_commands.command(
        name="стрим", description="Показать ссылки на русскоязычные трансляции турнира"
    )
    async def streams(self, interaction: discord.Interaction):
        await interaction.response.defer()

        try:
            html = await self.fetch_page_html()
        except Exception as e:
            await interaction.followup.send(f"Не удалось получить данные с Liquipedia: {e}")
            return

        loop = asyncio.get_event_loop()
        streams = await loop.run_in_executor(None, self._parse_streams, html)
        if not streams:
            await interaction.followup.send(
                "Русскоязычных трансляций для текущего турнира не найдено."
            )
            return

        lines = [f"**Русскоязычные трансляции** ({self.current_page.replace('_', ' ')})"]
        for s in streams:
            lines.append(f"\n__{s['section']}__")
            for link in s["links"]:
                lines.append(link)

        lines.append(f"\n-# Источник: Liquipedia · Турнир: {self.current_page.replace('_', ' ')}")
        await interaction.followup.send("\n".join(lines))

    # ---------- ИИ-прогнозист ----------

    def _team_matches_query(self, m: dict, query: str) -> bool:
        q = query.strip().lower()
        candidates = (m["team1"], m["team2"], m["team1_short"], m["team2_short"])
        return any(c.lower() == q for c in candidates if c)

    def _build_team_summary(self, matches: list[dict], query: str) -> dict | None:
        """Собирает статистику команды по уже сыгранным матчам турнира:
        реальное имя, счёт побед/поражений, список последних результатов.
        Имя команды резолвим по ЛЮБОМУ матчу (не обязательно завершённому) —
        иначе /прогноз отказывался работать для первого матча команды на
        турнире/стадии (0 завершённых игр), хотя это как раз тот случай,
        когда полезнее всего показать live-пики с esport.vision."""
        q = query.strip().lower()
        all_team_matches = [m for m in matches if self._team_matches_query(m, query)]
        if not all_team_matches:
            return None

        finished_matches = [m for m in all_team_matches if m["finished"]]
        name_source = finished_matches or all_team_matches
        name_source = sorted(name_source, key=lambda m: m["timestamp"] or 0)

        display_name = query
        first = name_source[0]
        for full, short in (
            (first["team1"], first["team1_short"]),
            (first["team2"], first["team2_short"]),
        ):
            if full.lower() == q or (short and short.lower() == q):
                display_name = full
                break

        finished_matches.sort(key=lambda m: m["timestamp"] or 0)
        wins, losses, recent = 0, 0, []
        for m in finished_matches:
            is_team1 = m["team1"].lower() == display_name.lower()
            own_score = m["score1"] if is_team1 else m["score2"]
            opp_score = m["score2"] if is_team1 else m["score1"]
            opponent = m["team2"] if is_team1 else m["team1"]
            try:
                won = int(own_score) > int(opp_score)
            except (ValueError, TypeError):
                continue
            if won:
                wins += 1
            else:
                losses += 1
            recent.append(f"{'W' if won else 'L'} vs {opponent} ({own_score}:{opp_score})")

        return {
            "name": display_name,
            "wins": wins,
            "losses": losses,
            "recent": recent[-5:],  # последние 5 матчей
        }

    def _find_head_to_head(self, matches: list[dict], team1: str, team2: str) -> list[str]:
        """Ищет прошлые очные встречи этих двух команд на турнире."""
        results = []
        for m in matches:
            if not m["finished"]:
                continue
            if self._team_matches_query(m, team1) and self._team_matches_query(m, team2):
                results.append(f"{m['team1']} {m['score1']} : {m['score2']} {m['team2']}")
        return results

    @discord.app_commands.command(
        name="прогноз", description="Шуточный ИИ-прогноз матча (не для ставок, просто ради интереса)"
    )
    @discord.app_commands.describe(
        команда1="Первая команда (можно сокращение)",
        команда2="Вторая команда (можно сокращение)",
    )
    async def predict(
        self, interaction: discord.Interaction, команда1: str, команда2: str
    ):
        await interaction.response.defer()

        ai_cog = self.bot.get_cog("AIChat")
        if ai_cog is None:
            await interaction.followup.send(
                "Модуль ИИ сейчас не загружен, прогноз сделать не получится."
            )
            return

        try:
            matches = await self.fetch_matches()
            html = await self.fetch_page_html()
        except Exception as e:
            await interaction.followup.send(f"Не удалось получить данные с Liquipedia: {e}")
            return

        team1_stats = self._build_team_summary(matches, команда1)
        team2_stats = self._build_team_summary(matches, команда2)

        if team1_stats is None or team2_stats is None:
            missing = команда1 if team1_stats is None else команда2
            await interaction.followup.send(
                f"Не нашёл команду «{missing}» на текущем турнире "
                f"({self.current_page.replace('_', ' ')}). Проверь название или переключи "
                f"турнир командой /турнир на нужную стадию (группа/плей-офф и т.п.)."
            )
            return

        head_to_head = self._find_head_to_head(matches, команда1, команда2)

        # Реальные пики/баны героев этого конкретного матча с esport.vision —
        # находим сами, без ручного ввода команд или id. Если матч сейчас не
        # идёт live (или esport.vision недоступен) — просто не добавляем блок,
        # /прогноз продолжает работать как раньше.
        esv_draft_context = None
        try:
            esv_draft_context = await self.fetch_esv_draft_context(
                team1_stats["name"], team2_stats["name"]
            )
        except Exception as e:
            print(f"[dota_tracker] Не удалось получить live-пики с esport.vision: {e}")

        # Общая мета-статистика героев турнира (не привязана к конкретной
        # команде — детальных данных по драфтам каждой команды в источнике
        # нет, но общий контекст меты добавляет прогнозу живости)
        loop = asyncio.get_event_loop()
        heroes = await loop.run_in_executor(None, self._parse_hero_stats, html)
        top_heroes = sorted(
            heroes, key=lambda h: int(h["picks"]) if h["picks"].isdigit() else 0, reverse=True
        )[:5]
        meta_line = "; ".join(
            f"{h['hero']} ({h['picks']} пиков, WR {h['winrate']})" for h in top_heroes
        )

        prompt_parts = [
            "Ты составляешь шуточный прогноз на матч по Dota 2 для друзей в Discord. "
            "Это не серьёзная аналитика и не ставки — просто весёлый прогноз с лёгким обоснованием, "
            "2-4 предложения. В конце явно укажи, какая команда, по-твоему, победит.",
            "",
            f"Команда 1: {team1_stats['name']}",
            f"Форма на турнире: {team1_stats['wins']}-{team1_stats['losses']} (побед-поражений)",
            f"Последние матчи: {'; '.join(team1_stats['recent']) or 'нет данных'}",
            "",
            f"Команда 2: {team2_stats['name']}",
            f"Форма на турнире: {team2_stats['wins']}-{team2_stats['losses']} (побед-поражений)",
            f"Последние матчи: {'; '.join(team2_stats['recent']) or 'нет данных'}",
        ]

        if meta_line:
            prompt_parts.append("")
            prompt_parts.append(f"Топ-5 популярных героев турнира (общая мета, не по командам): {meta_line}")

        if head_to_head:
            prompt_parts.append("")
            prompt_parts.append("Личные встречи на этом турнире: " + "; ".join(head_to_head))

        if esv_draft_context:
            prompt_parts.append("")
            prompt_parts.append(esv_draft_context)
            prompt_parts.append(
                "Учти эти реальные пики в прогнозе — это не общая статистика, "
                "а то, что команды выбрали именно в этой игре прямо сейчас."
            )

        prompt = "\n".join(prompt_parts)

        try:
            prediction = await ai_cog.ask_ai(prompt)
        except Exception as e:
            await interaction.followup.send(f"Не удалось получить прогноз от ИИ: {e}")
            return

        source_note = "Шуточный прогноз ИИ на основе статистики турнира"
        if esv_draft_context:
            source_note += " и live-пиков esport.vision"

        text = (
            f"🔮 **Прогноз: {team1_stats['name']} vs {team2_stats['name']}**\n\n"
            f"{prediction}\n\n"
            f"-# {source_note}, не финансовый совет и не гарантия результата"
        )
        await interaction.followup.send(text)

    # ---------- Общая мета героев по патчу ----------

    @discord.app_commands.command(
        name="патчмета", description="Общий винрейт героев по текущему патчу (не по турниру)"
    )
    @discord.app_commands.describe(
        позиция="Позиция: 1 (керри), 2 (мид), 3 (лес), 4 (саппорт), 5 (хард саппорт), или пусто для всех ролей",
        количество="Сколько героев показать (по умолчанию 15)",
    )
    async def patch_meta(
        self, interaction: discord.Interaction, позиция: str = "", количество: int = 15
    ):
        await interaction.response.defer()

        try:
            heroes = await self.fetch_patch_meta()
        except Exception as e:
            await interaction.followup.send(f"Не удалось получить данные с Dota2ProTracker: {e}")
            return

        pos = позиция.strip()
        if pos and pos not in D2PT_POSITION_NAMES:
            await interaction.followup.send(
                "Позиция должна быть числом от 1 до 5 (или пусто для общей статистики)."
            )
            return

        if pos:
            matches_key, winrate_key = f"pos {pos} matches", f"pos {pos} winrate"
            title_suffix = D2PT_POSITION_NAMES[pos]
        else:
            matches_key, winrate_key = "all matches", "all winrate"
            title_suffix = "все роли"

        # берём только героев с осмысленным числом игр на этой позиции,
        # сортируем по винрейту
        filtered = [h for h in heroes if h.get(matches_key, 0) >= 50]
        filtered.sort(key=lambda h: h.get(winrate_key, 0), reverse=True)
        top = filtered[:количество]

        if not top:
            await interaction.followup.send("Данных по этой позиции не нашлось.")
            return

        lines = [f"**Мета героев текущего патча** ({title_suffix})", "```"]
        lines.append(f"{'Герой':<20}{'WR':<8}{'Игр':<8}")
        for h in top:
            winrate_pct = h.get(winrate_key, 0) * 100
            lines.append(f"{h['displayName']:<20}{winrate_pct:<7.1f}%{h.get(matches_key, 0):<8}")
        lines.append("```")
        lines.append("-# Источник: Dota2ProTracker (текущий патч, не привязано к турниру)")
        await interaction.followup.send("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(DotaTracker(bot))
