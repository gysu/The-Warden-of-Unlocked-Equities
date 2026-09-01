import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
import pandas as pd
import requests
import yfinance as yf

# ==========================================
# 0. Discord Webhook 文字推播模組 (支援長訊息自動分段)
# ==========================================
def send_discord_table_message(webhook_url, content_text):
    """將文字/表格透過 Discord Webhook 發送，若超過 1900 字自動切段發送"""
    if not webhook_url:
        print('⚠️ 未設定 DISCORD_WEBHOOK_URL，跳過 Discord 發送。')
        return

    # Discord 單則上限 2000 字，留 buffer 設 1900
    chunks = []
    lines = content_text.split('\n')
    curr_chunk = ''

    for line in lines:
        if len(curr_chunk) + len(line) + 1 > 1900:
            chunks.append(curr_chunk)
            curr_chunk = line + '\n'
        else:
            curr_chunk += line + '\n'
    if curr_chunk:
        chunks.append(curr_chunk)

    for i, chunk in enumerate(chunks):
        payload = {'content': chunk}
        try:
            res = requests.post(
                webhook_url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=15,
            )
            if res.status_code in [200, 204]:
                print(f'✅ Discord 表格第 {i+1}/{len(chunks)} 段發送成功！')
            else:
                print(f'❌ Discord 發送失敗: {res.status_code} - {res.text}')
        except Exception as e:
            print(f'❌ Discord 連線異常: {e}')


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

        if pct <= -20:
            warning = '🔴跌逾20%'
        elif pct <= -15:
            warning = '🟠跌逾15%'
        elif pct >= 20:
            warning = '🟢漲逾20%'
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
# 6. 格式化 DataFrame 並發送到 Discord
# ==========================================
df_result = pd.DataFrame(results)

if not df_result.empty:
    # 1. 調整適合 Discord 等寬字體閱讀的精簡欄位
    display_cols = [
        '市場',
        '代號',
        '名稱',
        '出關日',
        '剩餘交易日',
        '最新收盤',
        '至今漲跌',
        '處置後月線斜率',
        '警示',
    ]
    df_discord = df_result[[c for c in display_cols if c in df_result.columns]]

    # 欄位簡化，縮短寬度避免手機破版
    df_discord = df_discord.rename(
        columns={
            '剩餘交易日': '剩餘',
            '最新收盤': '最新價',
            '處置後月線斜率': '月線斜率',
        }
    )

    # 2. 使用 tabulate 轉為終端機風格等寬表格 (grid 或 pipe)
    table_str = tabulate(
        df_discord, headers='keys', tablefmt='pipe', showindex=False
    )

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    message = (
        f'📊 **處置股分析結果彙整** ({now_str})\n'
        f'共追蹤 **{len(df_result)}** 檔標的：\n'
        f'```text\n{table_str}\n```'
    )

    print('\n===== 處置股分析結果 =====')
    print(table_str)

    webhook_url = os.getenv('DISCORD_WEBHOOK_URL')
    send_discord_table_message(webhook_url, message)
else:
    print('查無有效資料。')

