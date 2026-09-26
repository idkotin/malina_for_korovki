import csv, math, statistics as s, argparse
from collections import deque
parser=argparse.ArgumentParser(description='Replay archived t (ms), raw (kg), valid (0/1) CSV; no database writes.')
parser.add_argument('csv_path')
args=parser.parse_args()
rows=[]
for r in csv.DictReader(open(args.csv_path)):
    t=int(r['t'])/1000; x=float(r['raw'])
    if rows and t==rows[-1][0]: continue
    rows.append((t,x,r['valid']=='1'))
def filt(n,a,fast,threshold,hampel=False,source=None,hysteresis=False):
    out=[]; hist=deque(maxlen=n); rh=deque(maxlen=9); y=None; prev=None; display=None
    for t,x,v in (rows if source is None else source):
        if not v or not -1000<=x<=8000:
            hist.clear();rh.clear();y=None;display=None;out.append(None);prev=t;continue
        if prev is not None and t-prev>4: hist.clear();rh.clear();y=None;display=None
        prev=t
        raw=x
        if hampel:
            rh.append(x); m=s.median(rh); mad=s.median(abs(z-m) for z in rh)
            if abs(x-m)>1.4826*(mad or 1):x=m
        hist.append(x); m=s.median(hist)
        if y is None:y=m
        else:y+=(fast if abs(m-y)>=threshold else a)*(m-y)
        rounded=math.copysign(5*math.floor(abs(y)/5+.5),y)
        if not hysteresis or display is None or abs(y-display)>=3.5: display=rounded
        out.append(display)
    return out
# Fixed disjoint one-minute blocks. Independent raw medians in thirds identify plateaus.
blocks=[];start=0
for i in range(1,len(rows)):
    if rows[i][0]-rows[start][0]>=60 or rows[i][0]-rows[i-1][0]>4:
        part=rows[start:i]
        if len(part)>=20 and all(v and -1000<=x<=8000 for _,x,v in part):
            thirds=[s.median([x for _,x,_ in part[j*len(part)//3:(j+1)*len(part)//3]]) for j in range(3)]
            if max(thirds)-min(thirds)<=10: blocks.append((start,i))
        start=i
def metrics(out):
    spreads=[]; jumps=[]
    for a,b in blocks:
        q=sorted(x for x in out[a+10:b] if x is not None)
        if len(q)>10:spreads.append(q[int(.95*(len(q)-1))]-q[int(.05*(len(q)-1))])
    return {'plateaus':len(spreads),'median_p90_range':s.median(spreads),'p90_plateau_range':sorted(spreads)[int(.9*len(spreads))]}
print('rows',len(rows),'plateaus',len(blocks),'dt median',s.median(rows[i][0]-rows[i-1][0] for i in range(1,len(rows))))
for spec in [(3,.5,.8,30,False),(7,.1,.8,180,True),(5,.2,.65,60,False),(5,.15,.65,80,False),(5,.25,.65,50,False),(7,.25,.7,50,False),(5,.2,.7,30,False)]:
    print(spec,metrics(filt(*spec)))
new=filt(5,.2,.65,60,hysteresis=True)
print('final',metrics(new))
for dt in [.5,2]:
    for step in [25,100,500]:
        source=[(i*dt,1000+(step if i>=40 else 0),True) for i in range(150)]
        for name,spec in [('oldPi',(3,.5,.8,30,False)),('oldSite',(7,.1,.8,180,True)),('new',(5,.2,.65,60,False))]:
            result=filt(*spec,source=source,hysteresis=name=='new')
            delay=next((i*dt for i,v in enumerate(result[40:]) if v>=1000+.9*step),None)
            print('step90',dt,step,name,delay)
for day in ['2026-08-22','2026-08-23','2026-08-24']:
    from datetime import datetime,timezone
    selected=[(a,b) for a,b in blocks if datetime.fromtimestamp(rows[a][0],timezone.utc).isoformat().startswith(day)]
    original=blocks;blocks=selected
    if blocks:print(day,metrics(new))
    blocks=original


