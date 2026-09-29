import httpx, json
KEY=__import__('os').environ['OPENROUTER_API_KEY']
H={"Authorization":f"Bearer {KEY}"}; URL="https://openrouter.ai/api/v1/systemone"
def tok(state,qs):
    r=httpx.post(URL,headers=H,json={"model":"typesafe/jev-1.13","state":state,"questions":qs},timeout=60).json()
    return r["usage"]["input_tokens"]
P=("Rain fell minutes before the race, delaying the start, and as the track dried Massa established a lead of several seconds. "
   "More rain late in the race made the last few laps treacherous for the drivers, but could not prevent Massa from winning the Grand Prix.")
Q=lambda i:{"type":"noul","instructions":f"Is s{i} true?"}
res={}
res["A1 state='x', 1 noul"]=tok("x",{"q0":Q(0)})
res["A2 state='x', 2 noul"]=tok("x",{f"q{i}":Q(i) for i in range(2)})
res["A3 state='x', 8 noul"]=tok("x",{f"q{i}":Q(i) for i in range(8)})
res["A4 state=P, 1 noul"]=tok(P,{"q0":Q(0)})
res["A5 state=P, 8 noul"]=tok(P,{f"q{i}":Q(i) for i in range(8)})
res["A6 state={'s0':..,'s1':..}, 1 noul"]=tok({"sentences":{"s0":P.split('. ')[0],"s1":P.split('. ')[1]}},{"q0":Q(0)})
crit={"true":"The reader sees only one to four marked words per sentence. Without this phrase the core meaning is lost.","false":"The phrase can be skipped."}
res["A7 state='x', 1 noul + criteria"]=tok("x",{"q0":{**Q(0),"criteria":crit}})
S4=["Nothing important: a detail, elaboration or aside","Useful detail a skimming reader can skip","Important: removing it drops a key fact","Essential: the sentence breaks without it"]
res["A8 state='x', 1 score(4 levels)"]=tok("x",{"q0":{"type":"score","instructions":"Is s0 true?","criteria":S4}})
res["A9 state='x', 1 score(2 short levels)"]=tok("x",{"q0":{"type":"score","instructions":"Is s0 true?","criteria":["low","high"]}})
res["A10 state='x', 1 choice(2 opts)"]=tok("x",{"q0":{"type":"choice","instructions":"Is s0 true?","criteria":{"a":None,"b":None}}})
res["A11 state='x', 1 choice(8 opts)"]=tok("x",{"q0":{"type":"choice","instructions":"Is s0 true?","criteria":{f"opt{i}":None for i in range(8)}}})
zh="判断句子 s0 中的词语「修正案」是否属于快速浏览时必须看到的关键词。"
en='Is the phrase "修正案" in sentence s0 a key word a skimming reader must see?'
res["A12 noul zh instruction"]=tok("x",{"q0":{"type":"noul","instructions":zh}})
res["A13 noul en instruction"]=tok("x",{"q0":{"type":"noul","instructions":en}})
res["A14 noul minimal 's0: 修正案'"]=tok("x",{"q0":{"type":"noul","instructions":"s0: 修正案"}})
for k,v in res.items(): print(f"{v:6d}  {k}")
