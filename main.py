import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
import pandas as pd
import requests
import yfinance as yf

# ==========================================
# 0. Discord Webhook 推播模組
# ==========================================


def send_discord_embed(webhook_url, embeds, content_text=None):
    """直接透過 JSON 發送 Discord Embed 卡片通知"""
    if not webhook_url:
        print('⚠️ 未設定 DISCORD_WEBHOOK_URL，跳過 Discord 發送。')
        return

    payload = {'embeds': embeds}
    if content_text:
        payload['content'] = content_text

    try:
        res = requests.post(
            webhook_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=15,
        )

        if res.status_code in [200, 204]:
            print('✅ Discord 處置股推播發送成功！')
        else:
            print(f'❌ Discord 發送失敗: {res.status_code} - {res.text}')
    except Exception as e:
        print(f'❌ Discord 推播連線異常: {e}')


# ==========================================
# 1. 抓取處置股資料
# ==========================================

url = 'https://chengwaye.com/disposal-forecast'
headers = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,'
        ' like Gecko) Chrome/120.0.0.0 Safari/537.36'
    )
}

print('📥 開始抓取處置股清單...')
try:
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    dfs = pd.read_html(io.StringIO(response.text), match='出關日')
    df_raw = dfs[0]
except Exception as e:
    print(f'❌ 抓取處置股網頁失敗: {e}')
    df_raw = pd.DataFrame()


def clean_column_name(col):
    if isinstance(col, tuple):
        col = ''.join([str(c) for c in col])
    return re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', str(col))


if not df_raw.empty:
    df_raw.columns = [clean_column_name(c) for c in df_raw.columns]
    print('欄位：', list(df_raw.columns))

# ==========================================
# 2. 日期字串清理與交易日剩餘計算
# ==========================================

today = datetime.now()


def clean_date_str(date_val):
    if pd.isna(date_val):
        return ''
    cleaned = re.sub(r'[^\d\-/]', '', str(date_val)).strip()
    return cleaned.replace('/', '-')


def format_full_date(date_str, base_year):
    if not date_str:
        return ''
    parts = date_str.split('-')
    if len(parts) == 2:
        return f'{base_year}-{int(parts[0]):02d}-{int(parts[1]):02d}'
    elif len(parts) == 3:
        return f'{int(parts[0])}-{int(parts[1]):02d}-{int(parts[2]):02d}'
    return date_str


def trading_days_left(end_date):
    try:
        end_date = pd.to_datetime(end_date)
        if end_date.date() < today.date():
            return '已結束'
        days = pd.bdate_range(today.date(), end_date.date())
        remain = len(days) - 1
        if remain <= 0:
            return '今日出關'
        return f'{remain}日'
    except Exception:
        return '-'


# ==========================================
# 3. 股票清單整理
# ==========================================

market_dict = {'市': '上市', '櫃': '上櫃'}
stocks = []

if not df_raw.empty:
    for _, row in df_raw.iterrows():
        if pd.isna(row.get('代號')):
            continue

        code = str(row['代號']).strip()
        if not code.isdigit():
            continue

        market = str(row.get('所', '')).strip()
        yf_code = f'{code}.TW' if market == '市' else f'{code}.TWO'

        year = today.year
        start_clean = clean_date_str(row.get('開始', ''))
        out_clean = clean_date_str(row.get('出關日', ''))

        start = format_full_date(start_clean, year)
        out = format_full_date(out_clean, year)

        reason = row.get('處置原因', '')
        if isinstance(reason, pd.Series):
            reason = str(reason.iloc[0]).strip()
        else:
            reason = str(reason).strip() if pd.notna(reason) else ''

        stocks.append({
            '市場': market_dict.get(market, market),
            '代號': code,
            '名稱': row.get('名稱', ''),
            '撮合': row.get('撮合', ''),
            'YF_Code': yf_code,
            '處置起': start,
            '出關日': out,
            '剩餘交易日': trading_days_left(out) if out else '-',
            '處置原因': reason,
        })

df_stocks = pd.DataFrame(stocks)


# ==========================================
# 4. 月線斜率計算
# ==========================================


def calc_disposal_ma20_slope(stock_df, start_date):
    try:
        close = stock_df['Close']
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]

        close = close.dropna()
        ma20 = close.rolling(20).mean().dropna()

        if ma20.index.tz is not None:
            start_ts = pd.to_datetime(start_date).tz_localize(ma20.index.tz)
        else:
            start_ts = pd.to_datetime(start_date)

        ma20_after = ma20[ma20.index >= start_ts]
        if len(ma20_after) < 2:
            return '-'

        start_ma = ma20_after.iloc[0]
        last_ma = ma20_after.iloc[-1]

        if pd.isna(start_ma) or pd.isna(last_ma) or start_ma == 0:
            return '-'

        slope = (last_ma - start_ma) / start_ma * 100
        return round(slope, 2)
    except Exception:
        return '-'


# ==========================================
# 5. 開始分析
# ==========================================

results = []
print(f'📊 開始分析 {len(df_stocks)} 檔標的...')

for _, row in df_stocks.iterrows():
    ticker = row['YF_Code']
    start_dt = row['處置起']
    if not start_dt:
        continue

    try:
        stock_data = yf.download(
            ticker,
            start=(today - timedelta(days=150)).strftime('%Y-%m-%d'),
            end=(today + timedelta(days=2)).strftime('%Y-%m-%d'),
            progress=False,
            auto_adjust=True,
        )

        if stock_data.empty:
            continue

        if stock_data.index.tz is not None:
            start_ts = pd.to_datetime(start_dt).tz_localize(stock_data.index.tz)
        else:
            start_ts = pd.to_datetime(start_dt)

        df_after = stock_data[stock_data.index >= start_ts]
        if df_after.empty:
            continue

        close = df_after['Close']
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]

        close = close.dropna()
        if len(close) == 0:
            continue

        price_t0 = float(close.iloc[0])
        price_latest = float(close.iloc[-1])

        if price_t0 == 0 or pd.isna(price_t0) or pd.isna(price_latest):
            pct = 0.0
        else:
            pct = round((price_latest - price_t0) / price_t0 * 100, 2)

        ma_slope = calc_disposal_ma20_slope(stock_data, start_dt)

        if pct <= -10:
            warning = '🔴跌逾10%'
        elif pct <= -15:
            warning = '🟠跌逾15%'
        elif pct >= 10:
            warning = '🟢漲逾10%'
        else:
            warning = ''

        results.append({
            **row,
            'T日收盤': round(price_t0, 2),
            '最新收盤': round(price_latest, 2),
            '至今漲跌': f'{pct:+.2f}%',
            '處置後月線斜率': f'{ma_slope:+.2f}%' if ma_slope != '-' else '-',
            '警示': warning,
        })

    except Exception as e:
        print(f'處理 {ticker} 時發生錯誤: {e}')


# ==========================================
# 6. 直接發送 Discord 卡片通知 (含處置原因、T日收盤、至今漲跌)
# ==========================================

df_result = pd.DataFrame(results)

if not df_result.empty:
    focus_stocks = []
    normal_stocks = []

    for _, row in df_result.iterrows():
        rem = str(row['剩餘交易日'])
        warn = str(row['警示'])
        is_near_out = rem in ['今日出關', '1日', '2日', '3日']
        has_warn = warn != ''

        stock_info = {
            'code': row['代號'],
            'name': row['名稱'],
            'market': row['市場'],
            'rem': rem,
            't0_price': row['T日收盤'],
            'price': row['最新收盤'],
            'pct': row['至今漲跌'],
            'slope': row['處置後月線斜率'],
            'reason': row['處置原因'] if row['處置原因'] else '未提供',
            'warn': warn,
        }

        if is_near_out or has_warn:
            focus_stocks.append(stock_info)
        else:
            normal_stocks.append(stock_info)

    now_iso = datetime.now(timezone.utc).isoformat()
    fields = []

    # 1. 焦點標的 (出關倒數 <= 3 日 或 重大漲跌警示)
    for s in focus_stocks[:12]:
        warn_str = f" ｜ {s['warn']}" if s['warn'] else ''
        fields.append({
            'name': f"🚨 [{s['market']}] {s['code']} {s['name']} (剩 {s['rem']}){warn_str}",
            'value': (
                f"💰 **價格**: T日 `{s['t0_price']}` ➜ 最新 **{s['price']}** ({s['pct']})\n"
                f"📈 **月線斜率**: `{s['slope']}`\n"
                f"📝 **處置原因**: {s['reason']}"
            ),
            'inline': False,
        })

    # 2. 其餘處置中標的
    remain_slots = 25 - len(fields)
    if remain_slots > 0 and normal_stocks:
        for s in normal_stocks[:remain_slots]:
            fields.append({
                'name': f"📌 [{s['market']}] {s['code']} {s['name']} (剩 {s['rem']})",
                'value': (
                    f"💰 **價格**: T日 `{s['t0_price']}` ➜ 最新 **{s['price']}** ({s['pct']})\n"
                    f"📈 **月線斜率**: `{s['slope']}`\n"
                    f"📝 **處置原因**: {s['reason']}"
                ),
                'inline': False,
            })

    embed = {
        'title': '📋 處置股預告與走勢追蹤報告',
        'description': (
            f'共追蹤 **{len(df_result)}** 檔處置股。\n'
            f'🎯 **焦點標的 (倒數 $\\le$ 3日 / 重大漲跌)**：`{len(focus_stocks)}` 檔\n'
            f'⏳ **其餘處置中標的**：`{len(normal_stocks)}` 檔'
        ),
        'color': 0xF59E0B if focus_stocks else 0x3B82F6,
        'fields': fields,
        'footer': {'text': '處置股自動監控'},
        'timestamp': now_iso,
    }

    webhook_url = os.getenv('DISCORD_WEBHOOK_URL')
    send_discord_embed(webhook_url, embeds=[embed])
else:
    print('查無有效資料。')
