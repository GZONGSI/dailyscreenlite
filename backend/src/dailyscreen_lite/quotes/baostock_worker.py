"""Single bounded SDK session. stdout is exclusively a JSON response."""
import contextlib
import io
import json
import socket
import sys
from datetime import date, timedelta


FIELDS = "date,code,open,high,low,close,volume,amount,adjustflag,tradestatus"


def collect(client, request):
    start, end = date.fromisoformat(request["start"]), date.fromisoformat(request["end"])
    rows = []
    # <= 366 calendar days per query stays below SDK's 2000-row pagination edge.
    # Its next() can silently treat a failed next-page receive as normal EOF.
    while start <= end:
        stop = min(end, start + timedelta(days=365))
        result = client.query_history_k_data_plus(
            request["code"], FIELDS, start_date=start.isoformat(),
            end_date=stop.isoformat(), frequency="d", adjustflag="2",
        )
        if result.error_code != "0":
            return {"code": result.error_code, "message": result.error_msg}
        if result.fields != FIELDS.split(","):
            raise ValueError("Unexpected BaoStock fields")
        while result.error_code == "0" and result.next():
            rows.append(result.get_row_data())
        if result.error_code != "0":
            return {"code": result.error_code, "message": result.error_msg}
        start = stop + timedelta(days=1)
    return {"code": "0", "fields": FIELDS.split(","), "rows": rows}


def main():
    request = json.load(sys.stdin)
    socket.setdefaulttimeout(12)
    with contextlib.redirect_stdout(io.StringIO()):
        import baostock as bs
        logged_in = False
        try:
            login = bs.login()
            logged_in = login.error_code == "0"
            payload = collect(bs, request) if logged_in else {"code": login.error_code, "message": login.error_msg}
        except Exception as exc:
            payload = {"code": "client-error", "message": f"{type(exc).__name__}: {exc}"}
        finally:
            if logged_in:
                bs.logout()
    sys.stdout.write(json.dumps(payload, ensure_ascii=True))


if __name__ == "__main__":
    main()
