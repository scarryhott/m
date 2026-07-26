#!/usr/bin/env python3
from __future__ import annotations
from collections import deque
import math
import btc_recursive_backtest as bt

class FastSig:
    def __init__(self,p,c):
        self.f=self.s=self.pf=self.pp=p
        self.r=deque()
        self.s1=0.0
        self.s2=0.0
        self.af=2/(c.fast+1)
        self.asl=2/(c.slow+1)
        self.n=c.vw
    def up(self,p):
        self.pf=self.f
        x=math.log(p/self.pp)
        self.pp=p
        if len(self.r)>=self.n:
            old=self.r.popleft(); self.s1-=old; self.s2-=old*old
        self.r.append(x); self.s1+=x; self.s2+=x*x
        self.f=self.af*p+(1-self.af)*self.f
        self.s=self.asl*p+(1-self.asl)*self.s
        return self.f>self.s and self.f>=self.pf
    def sig(self):
        n=len(self.r)
        if n<3:return 0.0
        v=self.s2/n-(self.s1/n)**2
        return math.sqrt(max(0.0,v))

bt.Sig=FastSig
if __name__=='__main__':
    bt.main()
