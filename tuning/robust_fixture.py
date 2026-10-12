"""One coherent synthetic source across four intervals, no market observations."""
import math,csv,json
from pathlib import Path
from robust_data import DAY,BUCKET,INTERVALS,COLUMNS,iso,SOURCE

def make_source(root,days=850,proxy=False,start=1577836800000):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    # Generate at the finest accepted interval and aggregate the same prices.
    finest=[];n=days*96
    for i in range(n):
        o=100+8*math.sin(i/43)+4*math.sin(i/301)+i/500000
        c=100+8*math.sin((i+1)/43)+4*math.sin((i+1)/301)+(i+1)/500000
        finest.append([start+i*900000,o,max(o,c)+.2,min(o,c)-.2,c,10+i%7,start+(i+1)*900000-1])
    for interval,step in INTERVALS.items():
        parts=step//900000;rows=[]
        for i in range(0,n,parts):
            p=finest[i:i+parts];rows.append([p[0][0],p[0][1],max(x[2] for x in p),min(x[3] for x in p),p[-1][4],sum(x[5] for x in p),p[-1][6]])
        with (root/(interval+'.csv')).open('w',newline='') as f:w=csv.writer(f);w.writerow(COLUMNS);w.writerows(rows)
    fund=[];marks=[]
    for j in range(-1,days*3+1):
        t=start+j*BUCKET;mark=100+math.sin(j/21)
        marks.append([t,str(mark),str(mark+4),str(mark-4),str(mark),0,t+BUCKET-1,0,0,0,0,0])
        if j>=0:
            f={'time':t,'rate':.0001*(-1 if j%5==0 else 1),'mark_price':mark}
            if proxy and j<days*3//2:f.update(mark_price_source='binance-mark-kline-open-8h',mark_price_time=t)
            fund.append(f)
    (root/'funding.json').write_text(json.dumps(fund));(root/'mark.json').write_text(json.dumps(marks))
    (root/'metadata.json').write_text(json.dumps({'source':SOURCE,'symbol':'ETHUSDT'}));(root/'fixture.json').write_text(json.dumps({'synthetic':True,'real_market_rows':0}))
    return root

def plan(bundle,interval='1h',start_day=730,end_day=736,proxy=False):
    return {'version':'robust-eval-v1','bundle_dir':str(Path(bundle).resolve()),'interval':interval,'history_start':'2020-01-01T00:00:00Z','windows':[{'name':'synthetic','start':iso(1577836800000+start_day*DAY),'end':iso(1577836800000+end_day*DAY)}],'account_mode':'fixed-continuous-v1','funding_mode':'proxy-stress-v1' if proxy else 'exact','funding_scenarios':['central','proxy-adverse'] if proxy else ['central'],'cost_multipliers':[1,1.5,2]}
