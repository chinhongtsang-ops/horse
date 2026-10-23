import asyncio
from datetime import datetime
import hashlib
import json
import os
import re
import aiohttp
from bs4 import BeautifulSoup
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "race_data.json")
USERS_FILE = os.path.join(BASE_DIR, "users_data.json")
HTML_FILE = os.path.join(BASE_DIR, "index.html")

app = FastAPI(title="馬場實戰筆記 Pro")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

scraper_state = {
    "is_running": False,
    "progress": "閒置中",
    "last_updated": None,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
        " like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
}


# ==================== 1. 馬匹智能跑法與條件分析模組 ====================
def analyze_horse_performance(past_runs, current_draw, current_wt, current_dist):
  """根據馬匹近績自動計算：擅長跑法、路程、場地性能、檔位與負磅分析"""
  analysis = {
      "style": "均速跟前",  # 放頭 / 跟前 / 置中 / 後追
      "dist_apt": "初跑/同程考驗",
      "track_apt": "草地好地",
      "draw_note": "",
      "wt_note": "",
      "summary": "",
  }

  # 1. 跑法推算 (根據 past_runs 中的 pos，例如 "1-1-1-2" 或 "9-8-5-2")
  front_runs = 0
  mid_runs = 0
  back_runs = 0

  for r in past_runs:
    pos_str = r.get("pos", "")
    segments = re.findall(r"\d+", pos_str)
    if segments:
      first_pos = int(segments[0])
      if first_pos <= 2:
        front_runs += 1
      elif first_pos <= 5:
        mid_runs += 1
      else:
        back_runs += 1

  if front_runs >= 2:
    analysis["style"] = "前領/放頭"
  elif mid_runs >= 2 or (front_runs + mid_runs >= 2):
    analysis["style"] = "跟前守好位"
  elif back_runs >= 2:
    analysis["style"] = "置中留後追"
  else:
    analysis["style"] = "靈活機動"

  # 2. 檔位分析
  try:
    d = int(current_draw)
    if d <= 3:
      analysis["draw_note"] = f"{d}檔起步慳位，沿途易入內欄"
    elif d >= 10:
      analysis["draw_note"] = f"{d}檔外疊起步，需早段耗力切入"
    else:
      analysis["draw_note"] = f"{d}檔中規中矩，形勢靈活"
  except:
    analysis["draw_note"] = "檔位待定"

  # 3. 負磅分析
  try:
    w = int(re.search(r"\d+", str(current_wt)).group())
    if w >= 132:
      analysis["wt_note"] = f"頂磅({w}磅)，末段考驗韌力"
    elif w <= 118:
      analysis["wt_note"] = f"輕磅({w}磅)受惠，具末段衝刺優勢"
    else:
      analysis["wt_note"] = f"負磅合理({w}磅)"
  except:
    analysis["wt_note"] = "負磅正常"

  # 4. 場地與路程簡評
  best_places = [r for r in past_runs if str(r.get("place", "")).isdigit() and int(r.get("place")) <= 3]
  if best_places:
    best = best_places[0]
    analysis["dist_apt"] = f"曾入三甲 ({best.get('track','')} {best.get('dist','')}米)"
    analysis["track_apt"] = f"擅跑{best.get('going','好地')}"
  else:
    analysis["dist_apt"] = "仍需證明同程實力"
    analysis["track_apt"] = "場地性能廣"

  analysis["summary"] = f"跑法【{analysis['style']}】。{analysis['draw_note']}，{analysis['wt_note']}。"
  return analysis


# ==================== 2. 解析排位表真實班次與賽事名稱 (修復董事盃/二級賽Bug) ====================
def parse_race_header(soup, r_no):
  """精準提取真實場次標題、班次、路程及開跑時間，不重複二級賽"""
  race_title = f"第 {r_no} 場賽事"
  race_cls = "讓賽"
  race_dist = "-"
  race_time = "-"

  # 鎖定馬會排位表專用頂部資訊區塊
  info_div = (
      soup.find("div", class_="race_info")
      or soup.find("div", class_="f_fl race_title")
      or soup.find("div", class_="f_fs13")
  )
  text = info_div.get_text(" ", strip=True) if info_div else soup.get_text(" ", strip=True)

  # 1. 抓取班次 (優先精準匹配第一至五班、新馬賽、國際賽)
  cls_match = re.search(
      r"(第一班|第二班|第三班|第四班|第五班|新馬賽|國際一級賽|國際二級賽|國際三級賽|一級賽|二級賽|三級賽|公開賽)",
      text,
  )
  if cls_match:
    race_cls = cls_match.group(1)

  # 2. 抓取路程
  dist_match = re.search(r"(\d{4})米", text)
  if dist_match:
    race_dist = f"{dist_match.group(1)}米"

  # 3. 抓取開跑時間
  time_match = re.search(r"(\d{1,2}:\d{2})", text)
  if time_match:
    race_time = time_match.group(1)

  # 4. 抓取讓賽/盃賽名稱 (排除頁面大賽Banner)
  title_match = re.search(r"([^\s\d]{2,12}(?:讓賽|錦標|盃|賞|紀念盃|挑戰盃|大賽))", text)
  if title_match:
    race_title = title_match.group(1)
  else:
    race_title = f"第 {r_no} 場 ({race_cls})"

  return race_title, race_cls, race_dist, race_time


# ==================== 3. 爬蟲執行與馬匹完整分析整合 ====================
async def fetch_html(session, url):
  try:
    async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=8)) as resp:
      if resp.status == 200:
        return await resp.text()
  except:
    pass
  return None


async def scrape_horse_details(session, horse_id):
  if not horse_id:
    return [], [], {}
  h_url = f"https://racing.hkjc.com/racing/information/Chinese/Horse/Horse.aspx?HorseId={horse_id}"
  tw_url = f"https://racing.hkjc.com/racing/information/Chinese/Trackwork/TrackworkResult.aspx?HorseId={horse_id}"
  h_html, tw_html = await asyncio.gather(fetch_html(session, h_url), fetch_html(session, tw_url))

  past_runs, trackwork_list = [], []
  video_links = {
      "bt_video": f"https://racing.hkjc.com/racing/information/Chinese/Horse/BtResult.aspx?HorseId={horse_id}",
      "last_race_replay": "",
      "patrol_video": "https://racing.hkjc.com/racing/information/Chinese/Racing/ResultsAll.aspx",
  }

  if h_html:
    soup = BeautifulSoup(h_html, "html.parser")
    tables = soup.find_all("table", class_="bigborder")
    if tables:
      for row in tables[0].find_all("tr")[1:]:
        cols = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cols) >= 14 and cols[0].isdigit():
          replay_a = row.find("a", href=re.compile(r"ResultsAll|Replay", re.I))
          replay_url = ""
          if replay_a and replay_a.get("href"):
            h = replay_a["href"]
            replay_url = h if h.startswith("http") else f"https://racing.hkjc.com{h}"
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
          if len(past_runs) >= 4:
            break

  if tw_html:
    soup_tw = BeautifulSoup(tw_html, "html.parser")
    table_tw = soup_tw.find("table", class_="table_bd")
    if table_tw:
      for tr in table_tw.find_all("tr")[1:4]:
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

  return past_runs, trackwork_list, video_links


async def run_scraper_task():
  global scraper_state
  if scraper_state["is_running"]:
    return
  scraper_state["is_running"] = True
  scraper_state["progress"] = "正在掃描馬會最新賽期及海外賽事..."

  output_data = {"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "meetings": []}

  try:
    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
      # 1. 抓取首頁尋找本地及海外賽事 (ST/HV/S1/S2...)
      base_html = await fetch_html(session, "https://racing.hkjc.com/racing/information/Chinese/Racing/RaceCard.aspx")
      meetings = []
      if base_html:
        soup_base = BeautifulSoup(base_html, "html.parser")
        # 尋找所有帶有 RaceDate 及 Racecourse 的連結與選項
        found = set()
        for m in re.finditer(r"RaceDate=(\d{4}/\d{2}/\d{2})&(?:amp;)?Racecourse=([A-Z0-9]+)", base_html):
          d_str, c_str = m.group(1), m.group(2)
          key = f"{d_str}_{c_str}"
          if key not in found:
            found.add(key)
            is_sim = c_str.startswith("S") and c_str != "ST"
            v_name = "跑馬地" if c_str == "HV" else ("沙田" if c_str == "ST" else f"海外賽事 ({c_str})")
            meetings.append({
                "date": d_str,
                "course": c_str,
                "venue": v_name,
                "is_simulcast": is_sim,
                "label": f"{d_str} · {v_name}",
            })

      for m in meetings:
        r_date, r_course = m["date"], m["course"]
        is_sim = m["is_simulcast"]

        meeting_obj = {
            "id": f"{r_date.replace('/', '-')}_{r_course}",
            "date": r_date,
            "course": r_course,
            "is_simulcast": is_sim,
            "venue": m["venue"],
            "label": m["label"],
            "trackInfo": f"{m['venue']}跑道",
            "firstTime": "待定",
            "race_info": [],
            "races": {},
        }

        for r_no in range(1, 13):
          scraper_state["progress"] = f"正在抓取 {m['label']} 第 {r_no} 場..."
          url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/RaceCard.aspx?RaceDate={r_date}&Racecourse={r_course}&RaceNo={r_no}"
          html = await fetch_html(session, url)
          if not html:
            break

          soup = BeautifulSoup(html, "html.parser")
          race_table = soup.find("table", id="racecardlist") or soup.find("table", class_="starter")
          if not race_table:
            break

          # 提取精準場次名稱與班次 (修復董事盃)
          race_title, race_cls, race_dist, race_time = parse_race_header(soup, r_no)
          if r_no == 1 and race_time != "-":
            meeting_obj["firstTime"] = race_time

          # 提取馬匹
          tbody = race_table.find("tbody") or race_table
          rows = tbody.find_all("tr")
          parsed_horses = []

          for row in rows:
            cols = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cols) < 8 or not cols[0].isdigit():
              continue

            h_link = row.find("a", href=re.compile(r"HorseId=", re.I))
            h_name = h_link.get_text(strip=True) if h_link else (cols[3] if len(cols) > 3 else cols[2])
            h_id = ""
            if h_link:
              hm = re.search(r"HorseId=([A-Z0-9_]+)", h_link["href"], re.I)
              if hm:
                h_id = hm.group(1)

            wt = cols[5] if len(cols) > 5 else "-"
            jky = cols[6] if len(cols) > 6 else "-"
            dr = cols[8] if len(cols) > 8 else (cols[4] if is_sim else "-")
            tr = cols[9] if len(cols) > 9 else (cols[7] if is_sim else "-")
            rtg = cols[10] if len(cols) > 10 else "-"
            gear = cols[13] if len(cols) > 13 else ""

            parsed_horses.append({
                "no": int(cols[0]),
                "horse_id": h_id,
                "name": h_name,
                "draw": dr,
                "wt": wt,
                "jockey": jky,
                "trainer": tr,
                "rtg": rtg,
                "gear": gear,
            })

          # 抓取每匹馬近績並自動產生實戰分析
          async def enrich_horse(h):
            past, tw, vids = await scrape_horse_details(session, h["horse_id"])
            analysis = analyze_horse_performance(past, h["draw"], h["wt"], race_dist)
            h["past_runs"] = past
            h["trackwork"] = tw
            h["videos"] = vids
            h["analysis"] = analysis  # 加入距離、跑法、騎師、負磅、檔位分析
            return h

          horses = await asyncio.gather(*[enrich_horse(h) for h in parsed_horses])

          if horses:
            btn_label = f"{r_course}-{r_no}" if is_sim else f"第 {r_no} 場"
            meeting_obj["race_info"].append({
                "r": r_no,
                "btn_label": btn_label,
                "name": race_title,
                "time": race_time,
                "cls": race_cls,
                "dist": race_dist,
            })
            meeting_obj["races"][str(r_no)] = horses

        if meeting_obj["races"]:
          output_data["meetings"].append(meeting_obj)

    if output_data["meetings"]:
      with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
      scraper_state["progress"] = "更新完成！"
    else:
      scraper_state["progress"] = "未發現新排位表"

  except Exception as e:
    scraper_state["progress"] = f"錯誤: {str(e)}"
  finally:
    scraper_state["is_running"] = False


# ==================== 4. 跨裝置 API ====================
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
  return {"status": "started", "progress": "已啟動極速背景爬蟲..."}

@app.get("/api/scrape/status")
async def get_scrape_status():
  return scraper_state

if __name__ == "__main__":
  uvicorn.run(app, host="0.0.0.0", port=8000)
