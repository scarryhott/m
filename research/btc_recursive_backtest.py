#!/usr/bin/env python3
from __future__ import annotations
import csv,hashlib,io,itertools,json,math,statistics,time,urllib.request,zipfile
from dataclasses import dataclass,asdict,replace
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
import numpy as np
U=timezone.utc; B='https://data.binance.vision/data/futures/um'; S='BTCUSDT'; I='1m'
def ms(s): return int(datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()*1000)
def iso(x): return datetime.fromtimestamp(int(x)/1000,U).isoformat()
def get(u,n=4):
 for k in range(n):
  try:
   with urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'closure-bt'}),timeout=90) as r:return r.read()
  except Exception as e:
   if k==n-1: raise RuntimeError(f'{u}: {e}')
   time.sleep(k+1)
def arc(u,d,required=True):
 d.mkdir(parents=True,exist_ok=True); p=d/u.rsplit('/',1)[-1]
 if not p.exists():
  try:p.write_bytes(get(u))
  except RuntimeError:
   if required: raise
   return None
 try:
  z=get(u+'.CHECKSUM',2).decode().split()[0]; a=hashlib.sha256(p.read_bytes()).hexdigest()
  if z!=a: p.unlink(missing_ok=True); raise RuntimeError('checksum mismatch '+p.name)
 except RuntimeError: pass
 return p
def rows(p):
 with zipfile.ZipFile(p) as z:
  for n in z.namelist():
   if n.endswith('.csv'):
    with z.open(n) as f: yield from csv.reader(io.TextIOWrapper(f,encoding='utf-8'))
def num(s):
 try:float(s);return True
 except:return False
def kl(paths):
 o={}
 for p in paths:
  for r in rows(p):
   if r and num(r[0]) and len(r)>=5:o[int(float(r[0]))]=tuple(map(float,r[1:5]))
 return o
def fr(paths):
 o={}
 for p in paths:
  h=None
  for r in rows(p):
   if not r:continue
   if not num(r[0]):h=[x.lower() for x in r];continue
   try:
    ti=next((h.index(x) for x in ['calc_time','fundingtime','funding_time','time'] if h and x in h),0)
    ri=next((h.index(x) for x in ['last_funding_rate','fundingrate','funding_rate','rate'] if h and x in h),len(r)-1)
    x=float(r[ri]);t=int(float(r[ti]));
    if abs(x)<.1:o[t]=x
   except:pass
 return o
@dataclass
class M:
 t:np.ndarray;o:np.ndarray;h:np.ndarray;l:np.ndarray;c:np.ndarray;mo:np.ndarray;mh:np.ndarray;ml:np.ndarray;mc:np.ndarray;f:np.ndarray;meta:dict
 def sl(self,a,b):
  x=np.searchsorted(self.t,a);y=np.searchsorted(self.t,b,'right');v=[q[x:y] for q in [self.t,self.o,self.h,self.l,self.c,self.mo,self.mh,self.ml,self.mc,self.f]];return M(*v,self.meta)
 def rs(self,k):
  if k==1:return self
  g=self.t//(k*60000);a=np.flatnonzero(np.r_[True,g[1:]!=g[:-1]]);b=np.r_[a[1:],len(g)]
  t=self.t[a];o=self.o[a];c=self.c[b-1];mo=self.mo[a];mc=self.mc[b-1]
  return M(t,o,np.maximum.reduceat(self.h,a),np.minimum.reduceat(self.l,a),c,mo,np.maximum.reduceat(self.mh,a),np.minimum.reduceat(self.ml,a),mc,np.add.reduceat(self.f,a),{**self.meta,'resample':k})
def market(start,end,cache):
 ks=[];mk=[];fs=[]; first=date(end.year,end.month,1); mend=first-timedelta(days=1)
 y,m=start.year,start.month
 while date(y,m,1)<=mend:
  q=f'{y:04d}-{m:02d}';ks.append(arc(f'{B}/monthly/klines/{S}/{I}/{S}-{I}-{q}.zip',cache/'k'));mk.append(arc(f'{B}/monthly/markPriceKlines/{S}/{I}/{S}-{I}-{q}.zip',cache/'m'));x=arc(f'{B}/monthly/fundingRate/{S}/{S}-fundingRate-{q}.zip',cache/'f',False);fs+=([x] if x else[]);m+=1
  if m==13:y+=1;m=1
 d=max(start,first);de=min(end,datetime.now(U).date()-timedelta(days=1))
 while d<=de:
  q=str(d);x=arc(f'{B}/daily/klines/{S}/{I}/{S}-{I}-{q}.zip',cache/'k',False);y=arc(f'{B}/daily/markPriceKlines/{S}/{I}/{S}-{I}-{q}.zip',cache/'m',False);z=arc(f'{B}/daily/fundingRate/{S}/{S}-fundingRate-{q}.zip',cache/'f',False)
  if x and y:ks+=[x];mk+=[y]
  if z:fs+=[z]
  d+=timedelta(days=1)
 a=kl(ks);b=kl(mk);f=fr(fs);tt=sorted(set(a)&set(b));lo=int(datetime.combine(start,datetime.min.time(),tzinfo=U).timestamp()*1000);hi=int(datetime.combine(end,datetime.max.time(),tzinfo=U).timestamp()*1000);tt=[x for x in tt if lo<=x<=hi]
 if len(tt)<1e5:raise RuntimeError('too little data '+str(len(tt)))
 t=np.array(tt,np.int64);v=np.array([a[x] for x in tt]);w=np.array([b[x] for x in tt]);ff=np.zeros(len(t));idx={x:i for i,x in enumerate(tt)}
 for x,r in f.items():
  j=idx.get(x)
  if j is not None:ff[j]+=r
 gaps=int(np.sum(np.maximum(0,np.diff(t)//60000-1)))
 return M(t,*v.T,*w.T,ff,{'source':'Binance USD-M public archives','actual_start':iso(t[0]),'actual_end':iso(t[-1]),'minutes':len(t),'missing_minutes':gaps,'funding_events':int(np.count_nonzero(ff)),'archives':[len(ks),len(mk),len(fs)]})
@dataclass(frozen=True)
class C:
 origin:float=1000.;lev:float=50.;fee:float=.0004;slip:float=1.;mmr:float=.005;liqfee:float=.002;fund_mult:float=1.;act:float=.02;rev:float=.0015;give:float=.2;down:int=3;emerg:float=.008;fast:int=12;slow:int=48;confirm:int=4;cool:int=12;breakout:float=.0025;bv:float=1.5;vw:int=96;vh:int=32;vm:float=3.;amin:float=.005;budget:float=.1;reserve:float=.2;minlev:float=1.25;maxlev:float=10.;reentry:bool=True;close_liq:bool=False
@dataclass
class A:
 w:float;q:float;e:float;fees:float=0.;fund:float=0.;turn:float=0.;dead:bool=False
 def eq(self,p):return self.w+self.q*(p-self.e)
 def n(self,p):return abs(self.q)*p
 def lv(self,p):return self.n(p)/self.eq(p) if self.eq(p)>0 else math.inf
 def buf(self,p,c):return self.eq(p)-self.n(p)*(c.mmr+c.liqfee)
 def resize(self,target,p,c,floor=None):
  E=self.eq(p);N=self.n(p);old=self.q;sl=c.slip/1e4
  if target>=self.lv(p):
   add=max(0,(target*E-N)/(1+target*c.fee));px=p*(1+sl);dq=add/px;fee=add*c.fee;self.e=(self.q*self.e+dq*px)/(self.q+dq);self.q+=dq;self.w-=fee
  else:
   keep=max(0,target*(E-c.fee*N)/(1-target*c.fee));q2=min(self.q,keep/p);dq=self.q-q2;px=p*(1-sl);fee=dq*px*c.fee;self.w+=dq*(px-self.e)-fee;self.q=q2
  self.fees+=fee;self.turn+=abs(self.q-old)*p
  if floor is not None and self.eq(p)<floor-1e-7:return False
  return True
 def norm(self,p):x=self.eq(p);self.w=x;self.e=p;return x
class Sig:
 def __init__(self,p,c):self.f=self.s=self.pf=self.pp=p;self.r=[];self.af=2/(c.fast+1);self.asl=2/(c.slow+1);self.n=c.vw
 def up(self,p):
  self.pf=self.f;self.r.append(math.log(p/self.pp));self.r=self.r[-self.n:];self.pp=p;self.f=self.af*p+(1-self.af)*self.f;self.s=self.asl*p+(1-self.asl)*self.s;return self.f>self.s and self.f>=self.pf
 def sig(self):return statistics.pstdev(self.r) if len(self.r)>2 else 0.
def prop(a,p,b,s,c):
 adv=max(c.amin,c.vm*s.sig()*math.sqrt(c.vh));risk=c.budget/adv;liq=(1-c.reserve)/(adv+c.mmr+c.liqfee)
 lo,hi=a.lv(p),c.maxlev
 for _ in range(35):
  z=(lo+hi)/2;x=A(a.w,a.q,a.e);ok=x.resize(z,p,c,b)
  if ok and x.eq(p)>=b:lo=z
  else:hi=z
 return min(c.maxlev,risk,liq,lo),adv
def run(m,c,mode='50',events=False):
 p=m.o[0]
 if mode=='50':N=c.origin*c.lev;px=p*(1+c.slip/1e4);a=A(c.origin-N*c.fee,N/px,px,N*c.fee,0,N);phase='L';basis=c.origin;bp=p;reg=0
 else:a=A(c.origin,c.origin/p,p);phase='B';basis=c.origin;bp=p;reg=1
 s=Sig(p,c);peak=a.eq(p);pp=p;gp=peak;dd=0;mn=a.buf(m.mc[0],c);mx=a.lv(p);armed=False;uc=dc=0;cool=c.cool if phase=='B' else 0;pending=None;cl=re=0;elog=[];bg=[];lb=bb=0
 for i in range(len(m.t)):
  t=int(m.t[i]);op=float(m.o[i])
  if pending:
   typ,why,target=pending;pending=None
   if typ=='C':
    z=A(a.w,a.q,a.e);z.resize(1,op,c)
    if why=='emergency' or z.eq(op)>=basis:
     old=basis;a.resize(1,op,c);basis=a.norm(op);bp=op;reg+=1;cl+=1;phase='B';cool=c.cool;armed=False;uc=dc=0;peak=basis;pp=op;bg.append((basis/old-1)*100)
     if events:elog.append({'type':'closure','time':iso(t),'price':op,'reason':why,'old_basis':old,'new_basis':basis,'growth_pct':bg[-1]})
   elif c.reentry:
    target=min(target,prop(a,op,basis,s,c)[0]);z=A(a.w,a.q,a.e);ok=z.resize(target,op,c,basis)
    if ok and z.lv(op)>=c.minlev:a=z;re+=1;phase='L';peak=a.eq(op);pp=op;armed=False;uc=dc=0
  if m.f[i]:x=a.q*m.mo[i]*m.f[i]*c.fund_mult;a.w-=x;a.fund+=x
  lp=m.mc[i] if c.close_liq else m.ml[i]
  if a.buf(lp,c)<=0:a.w=max(0,a.eq(lp)-a.n(lp)*(c.mmr+c.liqfee));a.q=0;a.dead=True;break
  cp=float(m.c[i]);up=s.up(cp);E=a.eq(cp);gp=max(gp,E);dd=max(dd,(gp-E)/gp if gp else 0);mn=min(mn,a.buf(m.mc[i],c));mx=max(mx,a.lv(cp))
  if phase=='L':
   lb+=1;peak=max(peak,E);pp=max(pp,cp);dc=dc+1 if not up and s.f<s.s else 0;armed=armed or peak>=basis*(1+c.act);why=None;n=a.n(m.mc[i]);br=a.buf(m.mc[i],c)/n if n else 99
   if br<=c.emerg:why='emergency'
   elif armed:
    z=A(a.w,a.q,a.e);z.resize(1,cp,c);pres=z.eq(cp)>=basis;trail=max(basis,peak*(1-c.give))
    if pres and cp<=pp*(1-c.rev):why='reversal'
    elif pres and E<=trail:why='giveback'
    elif pres and dc>=c.down:why='trend'
   if why:pending=('C',why,1)
  else:
   bb+=1;cool=max(0,cool-1);uc=uc+1 if up else 0;bo=max(c.breakout,c.bv*s.sig()*math.sqrt(c.vh))
   if c.reentry and cool==0 and uc>=c.confirm and cp>=bp*(1+bo):
    tg,_=prop(a,cp,basis,s,c)
    if tg>=c.minlev:pending=('R','',tg)
 last=i;end=a.eq(m.c[last]) if not a.dead else a.w;yrs=max((m.t[last]-m.t[0])/(365.2425*86400000),1/365.2425)
 return {'status':'liquidated' if a.dead else 'completed','start':iso(m.t[0]),'end':iso(m.t[last]),'entry':float(m.o[0]),'final_price':float(m.c[last]),'final_equity':end,'return_pct':(end/c.origin-1)*100,'cagr_pct':((end/c.origin)**(1/yrs)-1)*100 if end>0 else -100,'max_drawdown_pct':dd*100,'closures':cl,'reentries':re,'fees':a.fees,'funding':a.fund,'turnover':a.turn,'max_leverage':mx,'min_buffer':mn,'basis':basis,'basis_growths':bg,'basis_monotonic':all(x>=-1e-8 for x in bg),'leveraged_pct':100*lb/max(1,lb+bb),'events':elog if events else None,'config':asdict(c)}
def spot(m):
 q=1000*(1-.001)/(m.o[0]*1.0001);v=q*m.c;pk=np.maximum.accumulate(v);return {'final_equity':float(v[-1]),'return_pct':float((v[-1]/1000-1)*100),'max_drawdown_pct':float(np.max((pk-v)/pk)*100)}
def scale(c,k):return replace(c,fast=max(2,round(c.fast/k)),slow=max(4,round(c.slow/k)),down=max(1,round(c.down/k)),confirm=max(1,round(c.confirm/k)),cool=max(1,round(c.cool/k)),vw=max(8,round(c.vw/k)),vh=max(1,round(c.vh/k)))
def optimize(m,base):
 grid=[]
 for a,r,b,l in itertools.product([.01,.02,.04],[.001,.002],[.001,.0025,.005],[5.,10.]):grid.append(replace(base,act=a,rev=r,breakout=b,maxlev=l))
 cut=np.linspace(0,len(m.t),5,dtype=int);out=[]
 for c in grid:
  vals=[]
  for x,y in zip(cut[:-1],cut[1:]):
   z=M(*[q[x:y] for q in [m.t,m.o,m.h,m.l,m.c,m.mo,m.mh,m.ml,m.mc,m.f]],m.meta);vals.append(run(z,scale(c,5),'1'))
  rr=[x['return_pct'] for x in vals];dd=[x['max_drawdown_pct'] for x in vals];dead=sum(x['status']=='liquidated' for x in vals);score=statistics.median(rr)+.25*min(rr)-.2*max(dd)-1000*dead;out.append({'score':score,'returns':rr,'worst_dd':max(dd),'dead':dead,'config':asdict(c)})
 out.sort(key=lambda x:x['score'],reverse=True);return out[:5],C(**out[0]['config'])
def main():
 out=Path('results/thorough_backtest');out.mkdir(parents=True,exist_ok=True);m=market(date(2021,7,1),date(2026,7,26),Path('data/binance'));entry=ms('2021-07-26T15:00:00Z');split=ms('2024-01-01T00:00:00Z');full=m.sl(entry,m.t[-1]);tr=m.sl(entry,split-1);te=m.sl(split,m.t[-1]);base=C();top,w=optimize(tr.rs(5),base)
 oos=[]
 for x in top:oos.append({'train':x,'oos':run(te,C(**x['config']),'1')})
 rec=run(full,w,'50',True);one=run(full,replace(w,reentry=False),'50',True);default=run(full,base,'50');static=run(full,replace(base,act=99,reentry=False,emerg=-99),'50');entries=[]
 for h in range(24):
  z=m.sl(ms(f'2021-07-26T{h:02d}:00:00Z'),m.t[-1]);r=run(z,w,'50');entries.append({'hour':h,'entry':r['entry'],'status':r['status'],'return_pct':r['return_pct'],'final_equity':r['final_equity'],'closures':r['closures'],'reentries':r['reentries']})
 stress=[]
 for n,c in [('base',w),('slip0',replace(w,slip=0)),('slip5',replace(w,slip=5)),('fee6',replace(w,fee=.0006)),('fee10',replace(w,fee=.001)),('mmr1',replace(w,mmr=.01)),('fund0',replace(w,fund_mult=0)),('fund2',replace(w,fund_mult=2)),('cap5',replace(w,maxlev=5)),('cap20',replace(w,maxlev=20)),('close_liq',replace(w,close_liq=True))]:
  r=run(full,c,'50');stress.append({'scenario':n,**{k:r[k] for k in ['status','final_equity','return_pct','max_drawdown_pct','closures','reentries','fees','funding']}})
 rep={'generated':datetime.now(U).isoformat(),'data':m.meta,'periods':{'entry':iso(full.t[0]),'end':iso(full.t[-1]),'split':iso(split)},'method':{'train':'36 configs, four 1x seeded blocks, 5m bars with scaled time constants','test':'frozen training winner on untouched 2024+ 1m data','execution':'signals at close, actions next open, mark-low liquidation, archived funding'},'winner':asdict(w),'results':{'recursive':rec,'one_way':one,'default':default,'static50':static,'spot':spot(full)},'top_train_oos':oos,'entry_timing':entries,'entry_survival_pct':100*sum(x['status']!='liquidated' for x in entries)/len(entries),'stress':stress,'limits':['1m OHLC has unknown intraminute ordering','fixed MMR, not historical tiers','no order-book depth or latency','not evidence of future profit']}
 (out/'report.json').write_text(json.dumps(rep,indent=2));
 for fn,rr in [('entry_timing.csv',entries),('stress.csv',stress)]:
  with (out/fn).open('w',newline='') as f:dw=csv.DictWriter(f,fieldnames=rr[0]);dw.writeheader();dw.writerows(rr)
 print(json.dumps({'data':m.meta,'winner':asdict(w),'recursive':{k:rec[k] for k in ['status','final_equity','return_pct','max_drawdown_pct','closures','reentries','fees','funding']},'one_way':{k:one[k] for k in ['status','final_equity','return_pct']},'static50':{k:static[k] for k in ['status','final_equity','return_pct']},'spot':spot(full),'entry_survival_pct':rep['entry_survival_pct']},indent=2))
if __name__=='__main__':main()
