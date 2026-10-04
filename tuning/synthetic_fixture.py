"""Isolated deterministic candles for engineering tests, never market evidence."""
import csv
import math
from datetime import datetime,timezone,timedelta
from pathlib import Path
import json

def make_fixture(root,interval="1h",days=110,score_folds=((60,65),(65,70),(70,75))):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    start=datetime(2024,1,1,tzinfo=timezone.utc);ms=int(start.timestamp()*1000)
    step=3600000 if interval=="1h" else 900000;perday=86400000//step
    rows=[]
    for i in range(days*perday):
        o=100+7*math.sin(i/11)+3*math.sin(i/41)+i/10000
        c=100+7*math.sin((i+1)/11)+3*math.sin((i+1)/41)+(i+1)/10000
        rows.append([ms+i*step,o,max(o,c)+.8,min(o,c)-.8,c,10+i%17,ms+(i+1)*step-1])
    daily=[]
    for d in range(days):
        part=rows[d*perday:(d+1)*perday]
        daily.append([part[0][0],part[0][1],max(x[2] for x in part),min(x[3] for x in part),part[-1][4],sum(x[5] for x in part),part[-1][6]])
    for name,data in ((interval,rows),("1d",daily)):
        with (root/(name+".csv")).open("w",newline="") as f:
            w=csv.writer(f);w.writerow(["open_time","open","high","low","close","volume","close_time"]);w.writerows(data)
    funding=[{"time":ms+i*28800000,"rate":.0001,"mark_price":100} for i in range(days*3+1)]
    (root/"funding.json").write_text(json.dumps(funding))
    def quality(data):
        return {"rows":len(data),"start":data[0][0],"end":data[-1][6]+1,**{n:0 for n in ("duplicates","gaps","out_of_order","nonfinite","bad_ohlc","bad_volume","misaligned","bad_close_time")}}
    # The existing loader requires this source identifier. fixture.json clearly
    # labels all generated values as synthetic, including synthetic funding.
    meta={"source":"https://fapi.binance.com (USD-M perpetual REST)","symbol":"ETHUSDT", "candles":{interval:quality(rows),"1d":quality(daily)},"funding_rows":len(funding)}
    (root/"metadata.json").write_text(json.dumps(meta))
    (root/"fixture.json").write_text(json.dumps({"synthetic":True,"generator":"synthetic_fixture.py","real_market_rows":0}))
    def day(n): return (start+timedelta(days=n)).isoformat().replace("+00:00","Z")
    plan={"schema_version":1,"dataset_dir":str(root.resolve()),"interval":interval,"history_start":day(1),
        "folds":[{"name":str(i),"start":day(a),"end":day(b)} for i,(a,b) in enumerate(score_folds)],
        "fee_bps":5,"slippage_bps":2,"min_trades":30,"min_positive_folds":2}
    path=root/"plan.json";path.write_text(json.dumps(plan))
    exposure={"schema_version":1,"symbol":"ETHUSDT","known_cache_end":day(days),"ranges":[{"start":day(0),"end":day(days),"use":"exposed_history","evidence":"synthetic deterministic generated fixture, no real data"}],"unused_final_data_available":False}
    (root/"exposure.json").write_text(json.dumps(exposure))
    return path
