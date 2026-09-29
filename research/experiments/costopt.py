"""比较三种实现的成本与结果一致性（英文，整篇 sample_en.txt）。
V0 上一版方案：每段一个请求；Score 每题带 4 级描述；state 重复；验证全部前缀。
V1 编码优化：全文一个请求/轮；span 在 state 中编号；判断标准只在 state 中写一次；题目只写编号；验证全部前缀。
V2 = V1 + 不考虑单词成分 + 括号内容按规则最先删除 + 二分查找验证。"""
import re, json, httpx, spacy, sys
nlp = spacy.load("en_core_web_sm")
KEY=__import__('os').environ['OPENROUTER_API_KEY']
H={"Authorization":f"Bearer {KEY}"}; URL="https://openrouter.ai/api/v1/systemone"
DELETABLE={"advmod","npadvmod","prep","agent","appos","acl","relcl","advcl","parataxis","conj","amod","nummod","poss"}
LEAD={"conj","advcl","appos","relcl","acl","npadvmod"}
PASS=0.7
LEDGER={}
def post(tag,state,qs):
    r=httpx.post(URL,headers=H,json={"model":"typesafe/jev-1.13","state":state,"questions":qs},timeout=120).json()
    if "answers" not in r: sys.exit(json.dumps(r)[:500])
    L=LEDGER.setdefault(tag,{"requests":0,"questions":0,"input_tokens":0,"cost":0.0})
    L["requests"]+=1; L["questions"]+=len(qs); L["input_tokens"]+=r["usage"]["input_tokens"]; L["cost"]+=r["usage"]["cost"]
    return r["answers"]

lines=[l.strip() for l in open(Path(__file__).resolve().parents[1] / 'data' / 'sample_en.txt').read().splitlines()[1:] if l.strip()]
paras=[]; gid=0
for pi,l in enumerate(lines):
    doc=nlp(l); ss=[]
    for s in doc.sents:
        sp=[]
        for t in s:
            if t.dep_ in DELETABLE:
                sub=list(t.subtree); a,b=sub[0].i,sub[-1].i
                if a-1>=s.start and doc[a-1].dep_ in {"cc","punct"} and t.dep_ in LEAD: a-=1
                sp.append({"a":a,"b":b,"dep":t.dep_,"text":doc[a:b+1].text})
        # 括号内容
        for m in re.finditer(r"\s*\([^)]*\)", s.text):
            st=s.start_char+m.start(); en=s.start_char+m.end()
            toks=[t.i for t in s if t.idx>=st and t.idx+len(t.text)<=en]
            if toks: sp.append({"a":toks[0],"b":toks[-1],"dep":"paren","text":doc[toks[0]:toks[-1]+1].text})
        for k,x in enumerate(sp): x["id"]=f"d{k}"
        ss.append({"sid":f"s{gid}","sent":s,"doc":doc,"spans":sp}); gid+=1
    paras.append(ss)
ALL=[s for p in paras for s in p]

def clean(txt):
    txt=re.sub(r"\s+([,.;:])",r"\1",txt); txt=re.sub(r"([,;])\s*(?=[,.;])","",txt); txt=re.sub(r"\(\s*\)","",txt)
    return re.sub(r"\s{2,}"," ",re.sub(r"^\W+","",txt)).strip()
def prefixes(s,order):
    drop=set(); seq=[]
    for x in order:
        rng=set(range(x["a"],x["b"]+1))
        if rng<=drop: continue
        drop|=rng; seq.append((frozenset(drop),clean("".join(t.text_with_ws for t in s["sent"] if t.i not in drop))))
    return seq
def words(t): return len(re.findall(r"\w+",t))

# ---------------- V0 ----------------
CRIT=["Nothing important: a detail, elaboration or aside","Useful detail a skimming reader can skip",
      "Important: removing it drops a key fact","Essential: the sentence breaks without it"]
def V0():
    res={}
    for p in paras:
        para=" ".join(s["sent"].text for s in p)
        state={"paragraph":para,"sentences":{s["sid"]:s["sent"].text for s in p}}
        qs={f'{s["sid"]}_{x["id"]}':{"type":"score","instructions":f'How much does the span "{x["text"]}" in sentence {s["sid"]} contribute to the core meaning of the paragraph?',"criteria":CRIT}
            for s in p for x in s["spans"]}
        ans=post("V0",state,qs) if qs else {}
        seqs={}
        for s in p:
            for x in s["spans"]: x["v0"]=ans[f'{s["sid"]}_{x["id"]}']["score"]
            order=sorted([x for x in s["spans"] if x["v0"]<2.0],key=lambda x:(x["v0"],x["b"]-x["a"]))
            seqs[s["sid"]]=prefixes(s,order)
        qs2={f"{sid}_{j}":{"type":"noul","instructions":{"task":f"Is the compressed version a grammatical English sentence that stays faithful to sentence {sid} (keeps its main point, no meaning distorted)?","compressed":txt}}
             for sid,seq in seqs.items() for j,(_,txt) in enumerate(seq)}
        a2=post("V0",state,qs2) if qs2 else {}
        for sid,seq in seqs.items():
            ok=[txt for j,(_,txt) in enumerate(seq) if a2[f"{sid}_{j}"]["noul"]>=PASS]
            res[sid]=ok[-1] if ok else next(s["sent"].text for s in ALL if s["sid"]==sid)
    return res

# ---------------- V1 / V2 共用 ----------------
GUIDE=("Two kinds of questions. (1) 'sN.dK' names span dK of sentence sN (see spans): answer yes if the span can be "
       "deleted and a skimming reader still gets the key point of the paragraph. (2) 'sN => text' gives a compressed "
       "version of sentence sN: answer yes only if it is a grammatical sentence that keeps the main point of sN without "
       "distorting its meaning.")
def state_for(ss, with_spans):
    st={"guideline":GUIDE,"sentences":{}}
    for s in ss:
        e={"text":s["sent"].text}
        if with_spans and s["spans"]: e["spans"]={x["id"]:x["text"] for x in s["spans"]}
        st["sentences"][s["sid"]]=e
    return st

def V1():
    tag="V1"
    ans=post(tag,state_for(ALL,True),{f'{s["sid"]}.{x["id"]}':{"type":"noul","instructions":f'{s["sid"]}.{x["id"]}'} for s in ALL for x in s["spans"]})
    seqs={}
    for s in ALL:
        for x in s["spans"]: x["v1"]=ans[f'{s["sid"]}.{x["id"]}']["noul"]
        order=sorted([x for x in s["spans"] if x["v1"]>=0.3],key=lambda x:(-x["v1"],x["b"]-x["a"]))
        seqs[s["sid"]]=prefixes(s,order)
    qs={f"{sid}_{j}":{"type":"noul","instructions":f"{sid} => {txt}"} for sid,seq in seqs.items() for j,(_,txt) in enumerate(seq)}
    a2=post(tag,state_for(ALL,False),qs)
    res={}
    for s in ALL:
        ok=[txt for j,(_,txt) in enumerate(seqs[s["sid"]]) if a2[f'{s["sid"]}_{j}']["noul"]>=PASS]
        res[s["sid"]]=ok[-1] if ok else s["sent"].text
    return res

def V2():
    tag="V2"
    multi=lambda x: x["dep"]=="paren" or x["b"]>x["a"]      # 只考虑多词成分
    for s in ALL: s["spans2"]=[x for x in s["spans"] if multi(x)]
    need=[(s,x) for s in ALL for x in s["spans2"] if x["dep"]!="paren"]
    st=state_for(ALL,False)
    for s in ALL:
        sp={x["id"]:x["text"] for x in s["spans2"] if x["dep"]!="paren"}
        if sp: st["sentences"][s["sid"]]["spans"]=sp
    ans=post(tag,st,{f'{s["sid"]}.{x["id"]}':{"type":"noul","instructions":f'{s["sid"]}.{x["id"]}'} for s,x in need}) if need else {}
    seqs={}
    for s in ALL:
        for x in s["spans2"]: x["v2"]=1.0 if x["dep"]=="paren" else ans[f'{s["sid"]}.{x["id"]}']["noul"]
        order=sorted([x for x in s["spans2"] if x["v2"]>=0.3],key=lambda x:(-x["v2"],x["b"]-x["a"]))
        seqs[s["sid"]]=prefixes(s,order)
    # 二分查找：每轮每句至多验证一个前缀，全文一个请求
    lo={sid:0 for sid in seqs}; hi={sid:len(seq) for sid,seq in seqs.items()}; passed={sid:[] for sid in seqs}
    stv=state_for(ALL,False)
    while True:
        qs={}
        for sid,seq in seqs.items():
            if lo[sid]<hi[sid]:
                mid=(lo[sid]+hi[sid]+1)//2; qs[f"{sid}_{mid}"]={"type":"noul","instructions":f"{sid} => {seq[mid-1][1]}"}
        if not qs: break
        a=post(tag,stv,qs)
        for q,v in a.items():
            sid,mid=q.rsplit("_",1); mid=int(mid)
            if v["noul"]>=PASS: lo[sid]=mid; passed[sid].append(mid)
            else: hi[sid]=mid-1
    res={}
    for s in ALL:
        sid=s["sid"]; res[sid]=seqs[sid][lo[sid]-1][1] if lo[sid]>0 else s["sent"].text
    return res


VTASK=lambda sid,t:{"type":"noul","instructions":{"task":f"Is the compressed version a grammatical English sentence that stays faithful to sentence {sid} (keeps its main point, no meaning distorted)?","compressed":t}}
def V3(min_words=12, targets=(0.85,0.70,0.55), tag="V3", retry=False):
    para=" ".join(s["sent"].text for s in ALL)
    rich={"paragraph":para,"sentences":{s["sid"]:s["sent"].text for s in ALL}}
    ok_span=lambda x: x["dep"]!="poss" and (x["dep"]=="paren" or x["b"]>x["a"])
    work=[s for s in ALL if words(s["sent"].text)>=min_words]
    for s in work: s["sp3"]=[x for x in s["spans"] if ok_span(x)]
    need=[(s,x) for s in work for x in s["sp3"] if x["dep"]!="paren"]
    st1={"guideline":"Each question quotes a span of sentence sN. Answer yes if the span can be deleted and a skimming reader still gets the key point.",**rich}
    ans=post(tag,st1,{f'{s["sid"]}.{x["id"]}':{"type":"noul","instructions":f'{s["sid"]}: "{x["text"]}"'} for s,x in need}) if need else {}
    qs={}; checks={}
    for s in work:
        for x in s["sp3"]: x["p3"]=1.0 if x["dep"]=="paren" else ans[f'{s["sid"]}.{x["id"]}']["noul"]
        order=sorted([x for x in s["sp3"] if x["p3"]>=0.5],key=lambda x:(-x["p3"],x["b"]-x["a"]))
        seq=prefixes(s,order); n0=words(s["sent"].text)
        picks=[]
        for tg in targets:      # 每个目标比例取最接近的前缀
            if not seq: break
            j=min(range(len(seq)),key=lambda j:abs(words(seq[j][1])/n0-tg))
            if j not in picks: picks.append(j)
        picks.sort(); checks[s["sid"]]=[(j,seq[j][1]) for j in picks]
        for j,t in checks[s["sid"]]: qs[f'{s["sid"]}_{j}']=VTASK(s["sid"],t)
    a2=post(tag,rich,qs) if qs else {}
    res={s["sid"]:s["sent"].text for s in ALL}
    seqs={s["sid"]:prefixes(s,sorted([x for x in s["sp3"] if x["p3"]>=0.5],key=lambda x:(-x["p3"],x["b"]-x["a"]))) for s in work}
    best={}
    for sid,cs in checks.items():
        ok=[(j,t) for j,t in cs if a2[f"{sid}_{j}"]["noul"]>=PASS]
        best[sid]=ok[-1][0] if ok else -1
        if ok: res[sid]=ok[-1][1]
    if retry:
        # 补验一轮：在最深通过点与下一个未通过检查点之间取中点；state 只含相关句子所在段落
        qs3={}; pend={}
        for sid,cs in checks.items():
            lo=best[sid]; fails=[j for j,_ in cs if j>lo and a2[f"{sid}_{j}"]["noul"]<PASS]
            hi=(min(fails) if fails else len(seqs[sid]))
            if hi-lo>=2:
                mid=(lo+hi)//2; pend[sid]=mid; qs3[f"{sid}_{mid}"]=VTASK(sid,seqs[sid][mid][1])
        if qs3:
            ps=[p for p in paras if any(s["sid"] in pend for s in p)]
            small={"paragraph":" ".join(s["sent"].text for p in ps for s in p),"sentences":{s["sid"]:s["sent"].text for p in ps for s in p}}
            a3=post(tag,small,qs3)
            for sid,mid in pend.items():
                if a3[f"{sid}_{mid}"]["noul"]>=PASS: res[sid]=seqs[sid][mid][1]
    return res
out={"V0":V0(),"V3-4t":V3(tag="V3-4t",targets=(0.85,0.7,0.55,0.4))}
tot=sum(words(s["sent"].text) for s in ALL)
print(f"{'':4s} {'请求':>4s} {'题目':>5s} {'输入tok':>8s} {'费用$':>10s} {'核心层字数占比':>12s} {'核心层与V0相同':>12s}")
for v in out:
    L=LEDGER[v]; core=sum(words(t) for t in out[v].values())
    same=sum(out[v][sid]==out["V0"][sid] for sid in out[v])
    print(f"{v:4s} {L['requests']:4d} {L['questions']:5d} {L['input_tokens']:8d} {L['cost']:10.6f} {core/tot:12.0%} {same:9d}/{len(ALL)}")
print()
for s in ALL:
    sid=s["sid"]; print(f"{sid} ORIG {s['sent'].text}")
    for v in out: print(f"    {v}: {out[v][sid]}")
