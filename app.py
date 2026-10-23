import asyncio
from datetime import datetime
import hashlib
import json
import os
import re
from bs4 import BeautifulSoup
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from playwright.async_api import async_playwright
from pydantic import BaseModel
import uvicorn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "race_data.json")
USERS_FILE = os.path.join(BASE_DIR, "users_data.json")
HTML_FILE = os.path.join(BASE_DIR, "index.html")

app = FastAPI(title="馬場實戰筆記 Pro Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 爬蟲運行狀態追蹤
scraper_state = {
    "is_running": False,
    "progress": "閒置中",
    "last_updated": None,
}


# ==================== 1. 跨裝置用戶與心水儲存系統 ====================
def load_users():
  if os.path.exists(USERS_FILE):
    try:
      with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)
    except Exception:
      return {}
  return {}


def save_users(users_data):
  with open(USERS_FILE, "w", encoding="utf-8") as f:
    json.dump(users_data, f, ensure_ascii=False, indent=2)


class LoginRequest(BaseModel):
  username: str
  pin: str


class SyncPicksRequest(BaseModel):
  username: str
  pin: str
  picks: dict


def hash_pin(pin: str) -> str:
  return hashlib.sha256(pin.encode("utf-8")).hexdigest()


@app.post("/api/login")
async def login_or_register(req: LoginRequest):
  username = req.username.strip().lower()
  if not username or not req.pin:
    raise HTTPException(status_code=400, detail="請輸入帳號名稱及密碼")

  users = load_users()
  pin_hash = hash_pin(req.pin)

  if username not in users:
    users[username] = {
        "display_name": req.username.strip(),
        "pin_hash": pin_hash,
        "picks": {},
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_users(users)
    return {
        "status": "registered",
        "message": "新帳號已建立並登入！",
        "username": username,
        "display_name": req.username.strip(),
        "picks": {},
    }

  if users[username]["pin_hash"] != pin_hash:
    raise HTTPException(status_code=401, detail="密碼錯誤，請重新輸入")

  return {
      "status": "ok",
      "message": "登入成功，已同步雲端心水！",
      "username": username,
      "display_name": users[username].get("display_name", username),
      "picks": users[username].get("picks", {}),
  }


@app.post("/api/sync_picks")
async def sync_user_picks(req: SyncPicksRequest):
  username = req.username.strip().lower()
  users = load_users()
  if username not in users or users[username]["pin_hash"] != hash_pin(req.pin):
    raise HTTPException(status_code=401, detail="未授權")

  users[username]["picks"] = req.picks
  users[username]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  save_users(users)
  return {"status": "ok", "updated_at": users[username]["updated_at"]}


# ==================== 2. 高速自動爬蟲引擎 (並發加速 + 精準標題) ====================
async def scrape_horse_details(context, horse_id):
  if not horse_id:
    return (
        [],
        [],
        {
            "last_race_replay": "",
            "bt_video": (
                "https://racing.hkjc.com/racing/information/Chinese/Horse/BtResult.aspx"
            ),
            "patrol_video": (
                "https://racing.hkjc.com/racing/information/Chinese/Racing/ResultsAll.aspx"
            ),
        },
    )

  page = await context.new_page()
  horse_url = f"https://racing.hkjc.com/racing/information/Chinese/Horse/Horse.aspx?HorseId={horse_id}"
  past_runs, trackwork_list = [], []
  video_links = {
      "last_race_replay": "",
      "bt_video": (
          f"https://racing.hkjc.com/racing/information/Chinese/Horse/BtResult.aspx?HorseId={horse_id}"
      ),
      "patrol_video": (
          "https://racing.hkjc.com/racing/information/Chinese/Racing/ResultsAll.aspx"
      ),
  }

  try:
    await page.goto(horse_url, wait_until="domcontentloaded", timeout=12000)
    await page.wait_for_timeout(400)
    soup = BeautifulSoup(await page.content(), "html.parser")

    tables = soup.find_all("table", class_="bigborder")
    if tables:
      for row in tables[0].find_all("tr")[1:]:
        cols = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cols) >= 14 and cols[0].isdigit():
          replay_a = row.find("a", href=re.compile(r"ResultsAll|Replay", re.I))
          replay_url = ""
          if replay_a and replay_a.get("href"):
            href = replay_a["href"]
            replay_url = (
                href
                if href.startswith("http")
                else f"https://racing.hkjc.com{href}"
            )
            if not video_links["last_race_replay"]:
              video_links["last_race_replay"] = replay_url

          past_runs.append({
              "place": cols[1],
              "date": cols[2],
              "track": f"{cols[3]} {cols[4]}",
              "dist": cols[5],
              "going": cols[6],
              "class": cols[7],
              "draw": cols[8],
              "rtg": cols[9],
              "jockey": cols[11],
              "lbw": cols[12],
              "odds": cols[13],
              "time": cols[15] if len(cols) > 15 else "-",
              "pos": cols[16] if len(cols) > 16 else "-",
              "replay_url": replay_url,
          })
          if len(past_runs) >= 3:
            break

    tw_url = f"https://racing.hkjc.com/racing/information/Chinese/Trackwork/TrackworkResult.aspx?HorseId={horse_id}"
    await page.goto(tw_url, wait_until="domcontentloaded", timeout=10000)
    await page.wait_for_timeout(300)
    tw_soup = BeautifulSoup(await page.content(), "html.parser")
    tw_table = tw_soup.find("table", class_="table_bd")
    if tw_table:
      for tr in tw_table.find_all("tr")[1:4]:
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) >= 5:
          trackwork_list.append({
              "date": tds[0],
              "type": tds[1],
              "track": tds[2],
              "time": tds[3],
              "rider": tds[4],
              "note": tds[5] if len(tds) > 5 else "",
          })
  except Exception:
    pass
  finally:
    await page.close()

  return past_runs, trackwork_list, video_links


async def discover_all_meetings(page):
  base_url = (
      "https://racing.hkjc.com/racing/information/Chinese/Racing/RaceCard.aspx"
  )
  await page.goto(base_url, wait_until="domcontentloaded", timeout=25000)
  await page.wait_for_timeout(1500)
  html = await page.content()
  soup = BeautifulSoup(html, "html.parser")

  candidates = {}
  for match in re.finditer(
      r"RaceDate=(\d{4}/\d{2}/\d{2})&(?:amp;)?Racecourse=(ST|HV|S\d+)", html
  ):
    r_date, r_course = match.group(1), match.group(2)
    key = f"{r_date}_{r_course}"
    if key not in candidates:
      venue_name = (
          "跑馬地"
          if r_course == "HV"
          else ("沙田" if r_course == "ST" else f"海外轉播 ({r_course})")
      )
      candidates[key] = {
          "date": r_date,
          "course": r_course,
          "venue": venue_name,
      }

  for opt in soup.find_all(["option", "a"]):
    txt = opt.get_text(" ", strip=True)
    val = opt.get("value", "") or opt.get("href", "")
    m_date = re.search(r"(\d{4}/\d{2}/\d{2}|\d{2}/\d{2}/\d{4})", val or txt)
    m_course = re.search(r"Racecourse=(ST|HV|S\d+)|\b(ST|HV|S\d+)\b", val or txt)
    if m_date and m_course:
      raw_d = m_date.group(1)
      fmt_date = (
          raw_d
          if raw_d.startswith("20")
          else f"{raw_d.split('/')[2]}/{raw_d.split('/')[1]}/{raw_d.split('/')[0]}"
      )
      course = m_course.group(1) or m_course.group(2)
      key = f"{fmt_date}_{course}"
      if course == "HV":
        venue = "跑馬地"
      elif course == "ST":
        venue = "沙田"
      else:
        clean_txt = re.sub(
            r"\d{4}[/年]\d{1,2}[/月]\d{1,2}日?|\d{2}/\d{2}/\d{4}", "", txt
        ).strip()
        venue = f"{course} {clean_txt}" if clean_txt else f"海外賽事 ({course})"
      candidates[key] = {"date": fmt_date, "course": course, "venue": venue}

  sorted_meetings = sorted(
      candidates.values(), key=lambda x: (x["date"], x["course"])
  )
  for m in sorted_meetings:
    m["label"] = f"{m['date']} · {m['venue']}"
  return sorted_meetings


def parse_horse_row(row, is_simulcast=False):
  cols = row.find_all("td")
  if len(cols) < 6:
    return None
  horse_no = cols[0].get_text(strip=True)
  if not horse_no.isdigit():
    return None

  horse_link = row.find("a", href=re.compile(r"HorseId=", re.I))
  horse_id = ""
  if horse_link:
    horse_name = horse_link.get_text(strip=True)
    hm = re.search(r"HorseId=([A-Z0-9_]+)", horse_link["href"], re.I)
    if hm:
      horse_id = hm.group(1)
  else:
    horse_name = ""
    for idx in [3, 2, 4]:
      if idx < len(cols):
        txt = cols[idx].get_text(strip=True)
        if txt and not re.match(r"^[\d\-\(\)\.]+$", txt):
          horse_name = txt
          break
    if not horse_name:
      return None

  text_cols = [c.get_text(strip=True) for c in cols]
  weight = text_cols[5] if len(text_cols) > 5 else "-"
  jockey = text_cols[6] if len(text_cols) > 6 else "-"
  draw = text_cols[8] if len(text_cols) > 8 else "-"
  trainer = text_cols[9] if len(text_cols) > 9 else "-"
  rating = text_cols[10] if len(text_cols) > 10 else "-"
  gear = text_cols[13] if len(text_cols) > 13 else ""

  if is_simulcast and len(text_cols) < 12:
    draw = text_cols[4] if len(text_cols) > 4 else "-"
    weight = text_cols[5] if len(text_cols) > 5 else "-"
    jockey = text_cols[6] if len(text_cols) > 6 else "-"
    trainer = text_cols[7] if len(text_cols) > 7 else "-"
    rating = text_cols[8] if len(text_cols) > 8 else "-"
    gear = text_cols[9] if len(text_cols) > 9 else ""

  return {
      "no": int(horse_no),
      "horse_id": horse_id,
      "name": horse_name,
      "draw": draw,
      "wt": weight,
      "jockey": jockey,
      "trainer": trainer,
      "rtg": rating,
      "gear": gear,
  }


async def process_single_horse(context, row, is_simulcast):
  parsed = parse_horse_row(row, is_simulcast=is_simulcast)
  if not parsed:
    return None

  past_runs, trackwork, videos = (
      await scrape_horse_details(context, parsed["horse_id"])
      if (parsed["horse_id"] and not is_simulcast)
      else (
          [],
          [],
          {
              "bt_video": (
                  "https://racing.hkjc.com/racing/information/Chinese/Horse/BtResult.aspx"
              ),
              "last_race_replay": (
                  "https://racing.hkjc.com/racing/information/Chinese/Racing/ResultsAll.aspx"
              ),
              "patrol_video": (
                  "https://racing.hkjc.com/racing/information/Chinese/Racing/ResultsAll.aspx"
              ),
          },
      )
  )

  parsed["past_runs"] = past_runs
  parsed["trackwork"] = trackwork
  parsed["videos"] = videos
  return parsed


async def run_scraper_task():
  global scraper_state
  if scraper_state["is_running"]:
    return
  scraper_state["is_running"] = True
  scraper_state["progress"] = "正在啟動瀏覽器並掃描馬會賽期..."

  output_data = {
      "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
      "meetings": [],
  }

  try:
    async with async_playwright() as p:
      browser = await p.chromium.launch(headless=True)
      context = await browser.new_context(
          user_agent=(
              "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
              " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
          )
      )
      page = await context.new_page()

      meetings = await discover_all_meetings(page)
      for m in meetings:
        r_date, r_course = m["date"], m["course"]
        is_simulcast = r_course.startswith("S") and r_course != "ST"
        meeting_obj = {
            "id": f"{r_date.replace('/', '-')}_{r_course}",
            "date": r_date,
            "course": r_course,
            "is_simulcast": is_simulcast,
            "venue": m["venue"],
            "label": m["label"],
            "trackInfo": (
                f"海外越洋轉播 ({r_course})"
                if is_simulcast
                else f"{m['venue']}草地"
            ),
            "firstTime": "待定",
            "race_info": [],
            "races": {},
        }

        for r_no in range(1, 13):
          scraper_state["progress"] = f"正在抓取 {m['label']} 第 {r_no} 場..."
          url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/RaceCard.aspx?RaceDate={r_date}&Racecourse={r_course}&RaceNo={r_no}"
          try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1000)
            soup = BeautifulSoup(await page.content(), "html.parser")

            race_table = soup.find("table", id="racecardlist") or soup.find(
                "table", class_="starter"
            )
            if not race_table:
              break

            page_text = soup.get_text(" ", strip=True)
            if not is_simulcast and r_no == 1:
              y, mo, da = r_date.split("/")
              if f"{y}年{int(mo)}月{int(da)}日" not in page_text:
                break

            # 精準抓取真實賽事名稱 (修復每場都顯示「董事盃」Bug)
            race_title = f"第 {r_no} 場賽事"
            title_block = (
                soup.find("div", class_="race_info")
                or soup.find("div", class_="f_fl race_title")
                or soup.find("div", class_="f_fl")
            )
            if title_block:
              t_text = title_block.get_text(" ", strip=True)
              t_match = re.search(
                  r"([^\s\d]{2,15}(?:讓賽|錦標|盃|賞|大賽|公開賽))", t_text
              )
              if t_match:
                race_title = t_match.group(1)

            cls_m = re.search(
                r"(第[一二三四五]班|公開賽|新馬賽|國際[一二三]級賽|[一二三]級賽|表列賽|讓賽)",
                page_text,
            )
            dist_m = re.search(r"(\d{4}米)", page_text)
            time_m = re.search(r"(\d{2}:\d{2})", page_text)

            r_time = time_m.group(1) if time_m else "-"
            if r_no == 1 and r_time != "-":
              meeting_obj["firstTime"] = r_time

            # 抓取馬匹列並改用並發加速 (大幅提升爬取速度)
            tbody = race_table.find("tbody") or race_table
            rows = tbody.find_all("tr")
            tasks = [
                process_single_horse(context, row, is_simulcast) for row in rows
            ]
            horse_results = await asyncio.gather(*tasks)
            horses = [h for h in horse_results if h is not None]

            if horses:
              race_btn_label = (
                  f"{r_course}-{r_no}" if is_simulcast else f"第 {r_no} 場"
              )
              meeting_obj["race_info"].append({
                  "r": r_no,
                  "btn_label": race_btn_label,
                  "name": race_title,
                  "time": r_time,
                  "cls": (
                      cls_m.group(1)
                      if cls_m
                      else ("越洋轉播" if is_simulcast else "-")
                  ),
                  "dist": dist_m.group(1) if dist_m else "-",
              })
              meeting_obj["races"][str(r_no)] = horses
          except Exception:
            continue

        if meeting_obj["races"]:
          output_data["meetings"].append(meeting_obj)

      await browser.close()

    if output_data["meetings"]:
      with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
      scraper_state["last_updated"] = output_data["updated_at"]
      scraper_state["progress"] = "更新完成！"
    else:
      scraper_state["progress"] = "未發現新排位表"
  except Exception as e:
    scraper_state["progress"] = f"錯誤: {str(e)}"
  finally:
    scraper_state["is_running"] = False


# ==================== 3. API 路由與自動伺服網頁 ====================
@app.get("/")
async def serve_index():
  return FileResponse(HTML_FILE)


@app.get("/api/data")
async def get_race_data():
  if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r", encoding="utf-8") as f:
      return JSONResponse(content=json.load(f))
  return JSONResponse(content={"updated_at": None, "meetings": []})


@app.post("/api/scrape")
async def trigger_scrape(background_tasks: BackgroundTasks):
  if scraper_state["is_running"]:
    return {"status": "running", "progress": scraper_state["progress"]}
  background_tasks.add_task(run_scraper_task)
  return {"status": "started", "progress": "已啟動背景爬蟲..."}


@app.get("/api/scrape/status")
async def get_scrape_status():
  return scraper_state


if __name__ == "__main__":
  uvicorn.run(app, host="0.0.0.0", port=8000)