import os
import json
import requests
import gspread
from zoneinfo import ZoneInfo
from datetime import datetime, date, timedelta
from oauth2client.service_account import ServiceAccountCredentials

META_API_VERSION = "v25.0"
GOOGLE_ADS_API_VERSION = "v24"
JST = ZoneInfo("Asia/Tokyo")

DEFAULT_META_WORKSHEET_NAME = "gitreport"
DEFAULT_GOOGLE_WORKSHEET_NAME = "gitreport2"

GOOGLE_CHANNELS = {
    "VIDEO": "YouTube",
    "DISPLAY": "Googleディスプレイ",
    "DEMAND_GEN": "デマンドジェネレーション",
}


def main():
    print("=== Start Meta + Google Export ===")

    config = load_secret()
    mask_sensitive_values(config)

    resolved = resolve_config(config)
    validate_config(resolved)

    meta_2m_ranges = get_monthly_ranges(months=2)
    meta_6m_ranges = get_monthly_ranges(months=6)
    google_2m_ranges = get_monthly_ranges(months=2)

    meta_rows = []
    for account_id in resolved["meta"]["account_ids"]:
        meta_rows.extend(fetch_meta_rows(
            act_id=account_id,
            token=resolved["meta"]["token"],
            ranges_2m=meta_2m_ranges,
            ranges_6m=meta_6m_ranges,
            daily_since=meta_2m_ranges[0]["since"],
            daily_until=meta_2m_ranges[-1]["until"],
        ))

    google_rows = fetch_google_rows(
        google_ads_conf=resolved["google_ads"],
        monthly_ranges=google_2m_ranges,
        daily_since=google_2m_ranges[0]["since"],
        daily_until=google_2m_ranges[-1]["until"],
    )

    spreadsheet = connect_spreadsheet(
        sheet_id=resolved["sheet"]["spreadsheet_id"],
        google_creds_dict=resolved["sheet"]["google_service_account"],
    )

    write_meta_sheet(
        spreadsheet=spreadsheet,
        sheet_name=resolved["sheet"]["meta_worksheet_name"],
        rows=sort_meta_rows(meta_rows),
    )

    write_google_sheet(
        spreadsheet=spreadsheet,
        sheet_name=resolved["sheet"]["google_worksheet_name"],
        rows=sort_google_rows(google_rows),
    )

    print(f"Meta rows written: {len(meta_rows)}")
    print(f"Google rows written: {len(google_rows)}")
    print("=== Completed ===")


def load_secret():
    secret_env = os.environ.get("APP_SECRET_JSON")
    if not secret_env:
        raise RuntimeError("APP_SECRET_JSON is not set")

    try:
        return json.loads(secret_env)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"APP_SECRET_JSON is invalid JSON: {e}") from e


# CodeQL対策：GitHubログへsecret値をprintしない
def mask_sensitive_values(config):
    return


def resolve_config(config):
    meta_conf = config.get("meta", {})
    google_ads_conf = config.get("google_ads", {})
    sheets_conf = config.get("sheets", {})

    account_ids = meta_conf.get("account_ids")
    if not account_ids:
        single_account_id = meta_conf.get("account_id") or config.get("m_act_id")
        account_ids = [single_account_id] if single_account_id else []

    google_service_account = config.get("gcp_service_account") or config.get("g_creds")

    return {
        "meta": {
            "token": meta_conf.get("token") or config.get("m_token"),
            "account_ids": account_ids,
        },
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
            "spreadsheet_id": sheets_conf.get("spreadsheet_id") or config.get("s_id"),
            "meta_worksheet_name": (
                sheets_conf.get("meta_worksheet_name")
                or DEFAULT_META_WORKSHEET_NAME
            ),
            "google_worksheet_name": (
                sheets_conf.get("google_worksheet_name")
                or DEFAULT_GOOGLE_WORKSHEET_NAME
            ),
            "google_service_account": normalize_google_service_account(
                google_service_account
            ),
        },
    }


def validate_config(resolved):
    required = {
        "meta.token": resolved["meta"]["token"],
        "meta.account_ids": resolved["meta"]["account_ids"],
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
    if fixed.get("private_key"):
        fixed["private_key"] = fixed["private_key"].replace("\\n", "\n")
    return fixed


def normalize_customer_id(value):
    if value is None:
        return None
    return str(value).strip().replace("-", "") or None


def normalize_meta_act_id(raw_act_id):
    cleaned = (
        str(raw_act_id)
        .replace("act=", "")
        .replace("act_", "")
        .replace("act", "")
        .strip()
    )
    return f"act_{cleaned}"


def add_months(base_date, months):
    month = base_date.month - 1 + months
    year = base_date.year + month // 12
    month = month % 12 + 1
    return date(year, month, 1)


def get_monthly_ranges(months):
    today_jst = datetime.now(JST).date()
    yesterday = today_jst - timedelta(days=1)
    current_month_start = date(today_jst.year, today_jst.month, 1)

    if yesterday < current_month_start:
        latest_month_start = add_months(current_month_start, -1)
    else:
        latest_month_start = current_month_start

    start_month = add_months(latest_month_start, -(months - 1))

    ranges = []
    current = start_month

    while current <= latest_month_start:
        next_month = add_months(current, 1)
        month_end = next_month - timedelta(days=1)

        since = current
        until = min(month_end, yesterday)

        if since <= until:
            ranges.append({
                "month": current.strftime("%Y-%m"),
                "since": since,
                "until": until,
            })

        current = next_month

    return ranges


def iter_dates(since, until):
    current = since
    while current <= until:
        yield current
        current += timedelta(days=1)


def fetch_meta_rows(act_id, token, ranges_2m, ranges_6m, daily_since, daily_until):
    normalized_act_id = normalize_meta_act_id(act_id)
    account_label = normalized_act_id
    rows = []

    day_items = fetch_meta_insights(
        act_id=normalized_act_id,
        token=token,
        since=daily_since,
        until=daily_until,
        level="account",
        time_increment="1",
        fields=["instagram_profile_visits"],
    )

    for item in day_items:
        value = extract_instagram_profile_visits(item)
        if value <= 0:
            continue

        day = item.get("date_start", "")
        rows.append(make_meta_row(
            scope="day",
            month=day[:7],
            day=day,
            account=account_label,
            campaign="",
            adset="",
            ad="",
            detail="",
            instagram_profile_visits=value,
        ))

    for month_range in ranges_6m:
        month_items = fetch_meta_insights(
            act_id=normalized_act_id,
            token=token,
            since=month_range["since"],
            until=month_range["until"],
            level="account",
            time_increment="monthly",
            fields=["instagram_profile_visits"],
        )

        for item in month_items:
            value = extract_instagram_profile_visits(item)
            if value <= 0:
                continue

            rows.append(make_meta_row(
                scope="month",
                month=month_range["month"],
                day="",
                account=account_label,
                campaign="",
                adset="",
                ad="",
                detail="",
                instagram_profile_visits=value,
            ))

    for month_range in ranges_2m:
        ad_items = fetch_meta_insights(
            act_id=normalized_act_id,
            token=token,
            since=month_range["since"],
            until=month_range["until"],
            level="ad",
            time_increment="monthly",
            fields=[
                "campaign_name",
                "adset_name",
                "ad_name",
                "instagram_profile_visits",
            ],
        )

        for item in ad_items:
            value = extract_instagram_profile_visits(item)
            if value <= 0:
                continue

            rows.append(make_meta_row(
                scope="ad",
                month=month_range["month"],
                day="",
                account=account_label,
                campaign=item.get("campaign_name", ""),
                adset=item.get("adset_name", ""),
                ad=item.get("ad_name", ""),
                detail="",
                instagram_profile_visits=value,
            ))

        for scope_name, breakdown_name in [
            ("campaign_gen", "gender"),
            ("campaign_age", "age"),
            ("campaign_pf", "publisher_platform"),
        ]:
            breakdown_items = fetch_meta_insights(
                act_id=normalized_act_id,
                token=token,
                since=month_range["since"],
                until=month_range["until"],
                level="campaign",
                time_increment="monthly",
                fields=[
                    "campaign_name",
                    "instagram_profile_visits",
                ],
                breakdowns=[breakdown_name],
            )

            for item in breakdown_items:
                value = extract_instagram_profile_visits(item)
                if value <= 0:
                    continue

                rows.append(make_meta_row(
                    scope=scope_name,
                    month=month_range["month"],
                    day="",
                    account=account_label,
                    campaign=item.get("campaign_name", ""),
                    adset="",
                    ad="",
                    detail=item.get(breakdown_name, ""),
                    instagram_profile_visits=value,
                ))

    return rows


def fetch_meta_insights(
    act_id,
    token,
    since,
    until,
    level,
    time_increment,
    fields,
    breakdowns=None,
):
    url = f"https://graph.facebook.com/{META_API_VERSION}/{act_id}/insights"

    params = {
        "access_token": token,
        "level": level,
        "time_range": json.dumps({
            "since": since.strftime("%Y-%m-%d"),
            "until": until.strftime("%Y-%m-%d"),
        }),
        "fields": ",".join(fields),
        "time_increment": time_increment,
        "limit": 5000,
    }

    if breakdowns:
        params["breakdowns"] = ",".join(breakdowns)

    all_rows = []

    while True:
        response = requests.get(url, params=params, timeout=180)

        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(
                f"Meta API request failed. act_id={act_id}, status={response.status_code}, body={truncate_text(response.text)}"
            ) from e

        payload = response.json()

        if "error" in payload:
            raise RuntimeError(
                f"Meta API error: {json.dumps(payload['error'], ensure_ascii=False)}"
            )

        all_rows.extend(payload.get("data", []))

        next_url = payload.get("paging", {}).get("next")
        if not next_url:
            break

        url = next_url
        params = None

    return all_rows


def extract_instagram_profile_visits(item):
    return to_int(item.get("instagram_profile_visits"))


def make_meta_row(
    scope,
    month,
    day,
    account,
    campaign,
    adset,
    ad,
    detail,
    instagram_profile_visits,
):
    return [
        "FB",
        scope,
        month or "",
        day or "",
        account or "",
        campaign or "",
        adset or "",
        ad or "",
        detail or "",
        to_int(instagram_profile_visits),
    ]


def fetch_google_rows(google_ads_conf, monthly_ranges, daily_since, daily_until):
    access_token = refresh_google_ads_access_token(
        client_id=google_ads_conf["client_id"],
        client_secret=google_ads_conf["client_secret"],
        refresh_token=google_ads_conf["refresh_token"],
    )

    rows = []

    for month_range in monthly_ranges:
        for api_channel, output_channel in GOOGLE_CHANNELS.items():
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

            if to_int(unique_users) <= 0:
                continue

            rows.append(make_google_row(
                scope="channel_month",
                month=month_range["month"],
                day="",
                channel=output_channel,
                campaign="",
                unique_users=unique_users,
            ))

    for target_day in iter_dates(daily_since, daily_until):
        month = target_day.strftime("%Y-%m")

        for api_channel, output_channel in GOOGLE_CHANNELS.items():
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

            if to_int(unique_users) <= 0:
                continue

            rows.append(make_google_row(
                scope="channel_day",
                month=month,
                day=target_day.strftime("%Y-%m-%d"),
                channel=output_channel,
                campaign="",
                unique_users=unique_users,
            ))

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
            unique_users = get_nested(item, "metrics", "uniqueUsers", default=0)

            if to_int(unique_users) <= 0:
                continue

            api_channel = get_nested(
                item,
                "campaign",
                "advertisingChannelType",
                default="",
            )
            output_channel = GOOGLE_CHANNELS.get(api_channel, api_channel)

            rows.append(make_google_row(
                scope="campaign_month",
                month=month_range["month"],
                day="",
                channel=output_channel,
                campaign=get_nested(item, "campaign", "name", default=""),
                unique_users=unique_users,
            ))

    return rows


def make_google_row(scope, month, day, channel, campaign, unique_users):
    return [
        channel or "",
        scope,
        month or "",
        day or "",
        channel or "",
        campaign or "",
        to_int(unique_users),
    ]


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


def write_meta_sheet(spreadsheet, sheet_name, rows):
    header = [[
        "media",
        "scope",
        "month",
        "day",
        "account",
        "campaign",
        "ad_set",
        "ad",
        "detail",
        "instagram_profile_visits",
    ]]

    write_rows(spreadsheet, sheet_name, header, rows)


def write_google_sheet(spreadsheet, sheet_name, rows):
    header = [[
        "media",
        "scope",
        "month",
        "day",
        "channel",
        "campaign",
        "unique users",
    ]]

    write_rows(spreadsheet, sheet_name, header, rows)


def write_rows(spreadsheet, sheet_name, header, rows):
    try:
        # CodeQL対策：シートは自動作成しない。事前に作成しておく。
        worksheet = spreadsheet.worksheet(sheet_name)

        worksheet.clear()
        worksheet.update("A1", header + rows, value_input_option="USER_ENTERED")

        print(f"Write success: {sheet_name} ({len(rows)} rows)")

    except gspread.WorksheetNotFound as e:
        raise RuntimeError(
            f"Worksheet not found: {sheet_name}. Please create this sheet manually."
        ) from e
    except Exception as e:
        raise RuntimeError(f"Write error ({sheet_name}): {repr(e)}") from e


def sort_meta_rows(rows):
    scope_order = {
        "day": 0,
        "month": 1,
        "ad": 2,
        "campaign_gen": 3,
        "campaign_age": 4,
        "campaign_pf": 5,
    }

    def sort_key(row):
        _media, scope, month, day, account, campaign, adset, ad, detail, _value = row
        date_value = day or month or ""
        date_num = int(date_value.replace("-", "")) if date_value else 0

        return (
            scope_order.get(scope, 999),
            -date_num,
            account,
            campaign,
            adset,
            ad,
            detail,
        )

    return sorted(rows, key=sort_key)


def sort_google_rows(rows):
    scope_order = {
        "channel_month": 0,
        "channel_day": 1,
        "campaign_month": 2,
    }

    def sort_key(row):
        media, scope, month, day, channel, campaign, _value = row
        date_value = day or month or ""
        date_num = int(date_value.replace("-", "")) if date_value else 0

        return (
            scope_order.get(scope, 999),
            -date_num,
            media,
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
