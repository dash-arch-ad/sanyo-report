import os
import json
import requests
import gspread
from zoneinfo import ZoneInfo
from datetime import datetime, date, timedelta
from oauth2client.service_account import ServiceAccountCredentials

GOOGLE_ADS_API_VERSION = "v23"
JST = ZoneInfo("Asia/Tokyo")
DEFAULT_WORKSHEET_NAME = "gitreport"

TARGET_CHANNELS = {
    "VIDEO": "YouTube",
    "DISPLAY": "Googleディスプレイ",
    "DEMAND_GEN": "デマンドジェネレーション",
}


def main():
    print("=== Start Google Ads Unique Users Export ===")

    config = load_secret()
    mask_sensitive_values(config)

    resolved = resolve_config(config)
    validate_config(resolved)

    monthly_ranges, daily_since, daily_until = get_target_date_ranges()

    print("Target monthly ranges:")
    for r in monthly_ranges:
        print(f"- {r['month']} / {r['since']} to {r['until']}")

    print(f"Target daily range: {daily_since} to {daily_until}")

    rows = fetch_google_ads_rows(
        google_ads_conf=resolved["google_ads"],
        monthly_ranges=monthly_ranges,
        daily_since=daily_since,
        daily_until=daily_until,
    )

    rows = sort_rows(rows)

    spreadsheet = connect_spreadsheet(
        sheet_id=resolved["sheet"]["spreadsheet_id"],
        google_creds_dict=resolved["sheet"]["google_service_account"],
    )

    write_to_sheet(
        spreadsheet=spreadsheet,
        sheet_name=resolved["sheet"]["worksheet_name"],
        rows=rows,
    )

    print(f"Total rows written: {len(rows)}")
    print("=== Completed ===")


def load_secret():
    secret_env = os.environ.get("APP_SECRET_JSON")
    if not secret_env:
        raise RuntimeError("APP_SECRET_JSON is not set")

    try:
        return json.loads(secret_env)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"APP_SECRET_JSON is invalid JSON: {e}") from e


def mask_sensitive_values(config):
    google_ads = config.get("google_ads", {})
    candidates = [
        google_ads.get("developer_token"),
        google_ads.get("client_id"),
        google_ads.get("client_secret"),
        google_ads.get("refresh_token"),
        google_ads.get("customer_id"),
        google_ads.get("login_customer_id"),
    ]

    for value in sorted(set([str(v).strip() for v in candidates if v])):
        if "\n" not in value:
            print(f"::add-mask::{value}")


def resolve_config(config):
    google_ads_conf = config.get("google_ads", {})
    sheets_conf = config.get("sheets", {})

    spreadsheet_id = sheets_conf.get("spreadsheet_id")
    worksheet_name = sheets_conf.get("worksheet_name") or DEFAULT_WORKSHEET_NAME

    google_service_account = (
        config.get("gcp_service_account")
        or config.get("g_creds")
    )
    google_service_account = normalize_google_service_account(google_service_account)

    return {
        "google_ads": {
            "developer_token": google_ads_conf.get("developer_token"),
            "client_id": google_ads_conf.get("client_id"),
            "client_secret": google_ads_conf.get("client_secret"),
            "refresh_token": google_ads_conf.get("refresh_token"),
            "customer_id": normalize_customer_id(google_ads_conf.get("customer_id")),
            "login_customer_id": normalize_customer_id(
                google_ads_conf.get("login_customer_id")
            ),
        },
        "sheet": {
            "spreadsheet_id": spreadsheet_id,
            "worksheet_name": worksheet_name,
            "google_service_account": google_service_account,
        },
    }


def validate_config(resolved):
    required = {
        "google_ads.developer_token": resolved["google_ads"]["developer_token"],
        "google_ads.client_id": resolved["google_ads"]["client_id"],
        "google_ads.client_secret": resolved["google_ads"]["client_secret"],
        "google_ads.refresh_token": resolved["google_ads"]["refresh_token"],
        "google_ads.customer_id": resolved["google_ads"]["customer_id"],
        "sheet.spreadsheet_id": resolved["sheet"]["spreadsheet_id"],
        "sheet.google_service_account": resolved["sheet"]["google_service_account"],
    }

    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(f"Missing required config keys: {', '.join(missing)}")


def normalize_google_service_account(creds):
    if not creds:
        return None

    fixed = dict(creds)
    private_key = fixed.get("private_key", "")
    if private_key:
        fixed["private_key"] = private_key.replace("\\n", "\n")
    return fixed


def normalize_customer_id(value):
    if value is None:
        return None
    value = str(value).strip().replace("-", "")
    return value or None


def add_months(base_date, months):
    month = base_date.month - 1 + months
    year = base_date.year + month // 12
    month = month % 12 + 1
    return date(year, month, 1)


def get_target_date_ranges():
    today_jst = datetime.now(JST).date()
    yesterday = today_jst - timedelta(days=1)

    this_month_start = date(today_jst.year, today_jst.month, 1)

    # 1日実行時は「当月1日〜前日」が存在しないため、前月を最新月として扱う
    if yesterday < this_month_start:
        latest_month_start = add_months(this_month_start, -1)
    else:
        latest_month_start = this_month_start

    start_month = add_months(latest_month_start, -2)

    monthly_ranges = []
    current = start_month

    while current <= latest_month_start:
        next_month = add_months(current, 1)
        month_end = next_month - timedelta(days=1)

        monthly_ranges.append({
            "month": current.strftime("%Y-%m-%d"),
            "since": current,
            "until": min(month_end, yesterday),
        })

        current = next_month

    daily_since = start_month
    daily_until = yesterday

    if daily_since > daily_until:
        raise RuntimeError("Target daily range is empty")

    return monthly_ranges, daily_since, daily_until


def iter_dates(since, until):
    current = since
    while current <= until:
        yield current
        current += timedelta(days=1)


def make_output_row(
    scope,
    month="",
    day="",
    channel="",
    campaign="",
    unique_users=0,
):
    return [
        "Google",
        scope,
        month,
        day,
        channel,
        campaign,
        to_int(unique_users),
    ]


def fetch_google_ads_rows(
    google_ads_conf,
    monthly_ranges,
    daily_since,
    daily_until,
):
    access_token = refresh_google_ads_access_token(
        client_id=google_ads_conf["client_id"],
        client_secret=google_ads_conf["client_secret"],
        refresh_token=google_ads_conf["refresh_token"],
    )

    rows = []

    # ① チャネル別×月別UU
    # UUは合算不可のため、チャネルごと・月ごとにsummaryRowで取得
    for month_range in monthly_ranges:
        for api_channel, output_channel in TARGET_CHANNELS.items():
            query = f"""
                SELECT
                  campaign.id,
                  metrics.unique_users
                FROM campaign
                WHERE campaign.status != 'REMOVED'
                  AND campaign.advertising_channel_type = '{api_channel}'
                  AND segments.date BETWEEN '{month_range["since"]:%Y-%m-%d}' AND '{month_range["until"]:%Y-%m-%d}'
                ORDER BY campaign.id
            """.strip()

            response = google_ads_search_stream(
                access_token=access_token,
                developer_token=google_ads_conf["developer_token"],
                customer_id=google_ads_conf["customer_id"],
                login_customer_id=google_ads_conf["login_customer_id"],
                query=query,
                summary_row_setting="SUMMARY_ROW_WITH_RESULTS",
            )

            unique_users = get_nested(
                response.get("summary_row"),
                "metrics",
                "uniqueUsers",
                default=0,
            )

            rows.append(
                make_output_row(
                    scope="channel_month",
                    month=month_range["month"],
                    day="",
                    channel=output_channel,
                    campaign="",
                    unique_users=unique_users,
                )
            )

    # ② チャネル別×日別UU
    # 日別UUも日付をまたいで合算しない前提で、1日×1チャネルずつ取得
    for target_day in iter_dates(daily_since, daily_until):
        for api_channel, output_channel in TARGET_CHANNELS.items():
            query = f"""
                SELECT
                  campaign.id,
                  metrics.unique_users
                FROM campaign
                WHERE campaign.status != 'REMOVED'
                  AND campaign.advertising_channel_type = '{api_channel}'
                  AND segments.date = '{target_day:%Y-%m-%d}'
                ORDER BY campaign.id
            """.strip()

            response = google_ads_search_stream(
                access_token=access_token,
                developer_token=google_ads_conf["developer_token"],
                customer_id=google_ads_conf["customer_id"],
                login_customer_id=google_ads_conf["login_customer_id"],
                query=query,
                summary_row_setting="SUMMARY_ROW_WITH_RESULTS",
            )

            unique_users = get_nested(
                response.get("summary_row"),
                "metrics",
                "uniqueUsers",
                default=0,
            )

            rows.append(
                make_output_row(
                    scope="channel_day",
                    month="",
                    day=target_day.strftime("%Y-%m-%d"),
                    channel=output_channel,
                    campaign="",
                    unique_users=unique_users,
                )
            )

    # ③ キャンペーン別×月別UU
    for month_range in monthly_ranges:
        query = f"""
            SELECT
              campaign.id,
              campaign.name,
              campaign.advertising_channel_type,
              metrics.unique_users
            FROM campaign
            WHERE campaign.status != 'REMOVED'
              AND campaign.advertising_channel_type IN ('VIDEO', 'DISPLAY', 'DEMAND_GEN')
              AND segments.date BETWEEN '{month_range["since"]:%Y-%m-%d}' AND '{month_range["until"]:%Y-%m-%d}'
            ORDER BY campaign.advertising_channel_type, campaign.id
        """.strip()

        response = google_ads_search_stream(
            access_token=access_token,
            developer_token=google_ads_conf["developer_token"],
            customer_id=google_ads_conf["customer_id"],
            login_customer_id=google_ads_conf["login_customer_id"],
            query=query,
        )

        for item in response["results"]:
            api_channel = get_nested(
                item,
                "campaign",
                "advertisingChannelType",
                default="",
            )

            rows.append(
                make_output_row(
                    scope="campaign_month",
                    month=month_range["month"],
                    day="",
                    channel=TARGET_CHANNELS.get(api_channel, api_channel),
                    campaign=get_nested(item, "campaign", "name", default=""),
                    unique_users=get_nested(item, "metrics", "uniqueUsers", default=0),
                )
            )

    return rows


def refresh_google_ads_access_token(client_id, client_secret, refresh_token):
    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=120,
    )

    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(
            f"Google OAuth token refresh failed. status={response.status_code}, body={truncate_text(response.text)}"
        ) from e

    payload = response.json()
    access_token = payload.get("access_token")

    if not access_token:
        raise RuntimeError(
            f"Google OAuth token refresh returned no access_token: {payload}"
        )

    print("Google Ads OAuth token refreshed successfully")
    return access_token


def google_ads_search_stream(
    access_token,
    developer_token,
    customer_id,
    login_customer_id,
    query,
    summary_row_setting=None,
):
    url = (
        f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}/customers/"
        f"{customer_id}/googleAds:searchStream"
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": developer_token,
        "Content-Type": "application/json",
    }

    if login_customer_id:
        headers["login-customer-id"] = login_customer_id

    body = {"query": query}

    if summary_row_setting:
        body["summaryRowSetting"] = summary_row_setting

    response = requests.post(url, headers=headers, json=body, timeout=120)

    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(
            f"Google Ads API request failed. status={response.status_code}, body={truncate_text(response.text)}"
        ) from e

    payload = response.json()

    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(
            f"Google Ads API error: {truncate_text(json.dumps(payload['error'], ensure_ascii=False))}"
        )

    if not isinstance(payload, list):
        raise RuntimeError(
            f"Google Ads API unexpected response shape: {truncate_text(json.dumps(payload, ensure_ascii=False))}"
        )

    all_rows = []
    summary_row = None

    for chunk in payload:
        all_rows.extend(chunk.get("results", []))

        if chunk.get("summaryRow"):
            summary_row = chunk["summaryRow"]

    return {
        "results": all_rows,
        "summary_row": summary_row,
    }


def connect_spreadsheet(sheet_id, google_creds_dict):
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]

        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            google_creds_dict,
            scope,
        )

        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(sheet_id)

        print("Google Sheets connected successfully")
        return spreadsheet

    except Exception as e:
        raise RuntimeError(f"Google Sheets connection error: {repr(e)}") from e


def write_to_sheet(spreadsheet, sheet_name, rows):
    header = [[
        "media",
        "scope",
        "month",
        "day",
        "channel",
        "campaign",
        "unique users",
    ]]

    try:
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=sheet_name,
                rows=1000,
                cols=7,
            )

        worksheet.clear()

        output = header + rows
        worksheet.update("A1", output)

        print(f"Write success: {sheet_name} ({len(rows)} rows)")

    except Exception as e:
        raise RuntimeError(f"Write error ({sheet_name}): {repr(e)}") from e


def sort_rows(rows):
    scope_order = {
        "channel_month": 0,
        "channel_day": 1,
        "campaign_month": 2,
    }

    def sort_key(row):
        _media, scope, month, day, channel, campaign, _unique_users = row

        date_value = month or day or ""
        date_num = int(date_value.replace("-", "")) if date_value else 0

        return (
            scope_order.get(scope, 999),
            -date_num,
            channel,
            campaign,
        )

    return sorted(rows, key=sort_key)


def get_nested(data, *keys, default=""):
    current = data

    for key in keys:
        if not isinstance(current, dict):
            return default

        current = current.get(key)

        if current is None:
            return default

    return current


def to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def truncate_text(value, limit=800):
    value = str(value)

    if len(value) <= limit:
        return value

    return value[:limit] + "...(truncated)"


if __name__ == "__main__":
    main()
