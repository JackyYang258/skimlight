"""在人工标注集上比较题目写法：准确率 vs 每题输入 token。每种写法跑 2 次取平均。"""
import httpx, json, statistics
KEY=__import__('os').environ['OPENROUTER_API_KEY']
H={"Authorization":f"Bearer {KEY}"}; URL="https://openrouter.ai/api/v1/systemone"
def post(state,qs):
    r=httpx.post(URL,headers=H,json={"model":"typesafe/jev-1.13","state":state,"questions":qs},timeout=120).json()
    return r["answers"], r["usage"]["input_tokens"]
S={"s0":"The 2008 Brazilian Grand Prix (formally the Formula 1 Grande Prêmio do Brasil 2008) was a Formula One motor race held on 2 November 2008 at the Autódromo José Carlos Pace (Interlagos) in São Paulo, Brazil.",
"s1":"It was the eighteenth and final race of the 2008 Formula One World Championship.",
"s2":"Ferrari driver Felipe Massa won the 71-lap race from pole position; this was the last of Massa's 11 Grand Prix wins.",
"s3":"Fernando Alonso finished second in a Renault, and Massa's teammate Kimi Räikkönen finished third.",
"s4":"Massa started the race alongside Toyota driver Jarno Trulli.",
"s5":"Massa's teammate Räikkönen began from third next to McLaren driver Lewis Hamilton.",
"s6":"Rain fell minutes before the race, delaying the start, and as the track dried Massa established a lead of several seconds.",
"s7":"More rain late in the race made the last few laps treacherous for the drivers, but could not prevent Massa from winning the Grand Prix.",
"s8":"Sebastian Vettel of Toro Rosso finished in fourth place behind Alonso and Räikkönen.",
"s9":"Hamilton passed Toyota's Timo Glock in the final corners of the race to finish fifth, securing him the points needed to take the Drivers' Championship.",
"s10":"Hamilton received praise from many in the Formula One community, including former champions Damon Hill and Michael Schumacher.",
"s11":"The McLaren driver also received official congratulations from Queen Elizabeth II and British prime minister Gordon Brown.",
"s12":"Massa's win and Räikkönen's third place helped Ferrari win the Constructors' Championship.",
"s13":"The Grand Prix was 13-time Grand Prix winner David Coulthard's final race; the Scot retired after 246 race starts."}
# 验证集：(句子, 压缩版本, 通顺且忠实?)
VER=[("s6","Rain fell, and as the track dried Massa established a lead.",1),
("s6","Rain fell.",0),
("s6","Rain fell , , and as the track dried Massa established a lead .",0),
("s7","More rain made the last few laps treacherous, but could not prevent Massa from winning the Grand Prix.",1),
("s7","More rain late in the race made the last laps, but could not prevent Massa from winning the Grand Prix.",0),
("s7","rain made the laps, but could not prevent Massa from winning the Grand Prix.",0),
("s12","win helped Ferrari win Championship.",0),
("s12","Massa's win and Räikkönen's third place helped Ferrari win the Constructors' Championship.",1),
("s5","teammate Räikkönen began from third next to McLaren driver Lewis Hamilton.",0),
("s5","Räikkönen began from third next to Lewis Hamilton.",1),
("s9","Hamilton passed Timo Glock.",0),
("s9","Hamilton passed Timo Glock to finish fifth, securing him the points needed to take the Drivers' Championship.",1),
("s9","Hamilton passed Timo Glock, securing him the points needed to take Championship.",0),
("s11","The McLaren driver received congratulations from Queen Elizabeth II.",1),
("s0","The 2008 Brazilian Grand Prix was a Formula One motor race held at the Autódromo José Carlos Pace.",1),
("s0","The 2008 Brazilian Grand Prix was a Formula One motor race held on November 2008 at the Autódromo José Carlos Pace.",0),
("s2","Ferrari driver Felipe Massa won the 71-lap race; this was the last of Massa's 11 Grand Prix wins.",1),
("s8","Sebastian Vettel finished in fourth place.",1),
("s1","It was the eighteenth race of the 2008 Formula One World Championship.",0),
("s13","The Grand Prix was 13-time Grand Prix winner David Coulthard's final race; the Scot retired.",1)]
# 成分集：(句子, 成分, 可删?)
SPN=[("s0","(formally the Formula 1 Grande Prêmio do Brasil 2008)",1),("s0","(Interlagos)",1),
("s2","from pole position",1),("s3","in a Renault",1),("s4","alongside Toyota driver Jarno Trulli",1),
("s6","minutes before the race",1),("s6","delaying the start",1),("s6","of several seconds",1),
("s7",", but could not prevent Massa from winning the Grand Prix",0),("s7","treacherous for the drivers",0),
("s8","of Toro Rosso",1),("s9","to finish fifth",0),("s9",", securing him the points needed to take the Drivers' Championship",0),
("s10",", including former champions Damon Hill and Michael Schumacher",1),("s11","from Queen Elizabeth II and British prime minister Gordon Brown",0),
("s13","after 246 race starts",1)]
para=" ".join(S.values())
VFMT={
 "V-a 每题完整任务说明(JSON)": (lambda: {"paragraph":para,"sentences":S},
     lambda sid,t:{"type":"noul","instructions":{"task":f"Is the compressed version a grammatical English sentence that stays faithful to sentence {sid} (keeps its main point, no meaning distorted)?","compressed":t}}),
 "V-b 同上，state 去重": (lambda: {"sentences":S},
     lambda sid,t:{"type":"noul","instructions":{"task":f"Is the compressed version a grammatical English sentence that stays faithful to sentence {sid} (keeps its main point, no meaning distorted)?","compressed":t}}),
 "V-c 短说明": (lambda: {"sentences":S},
     lambda sid,t:{"type":"noul","instructions":f'Grammatical, and keeps the main point of {sid}: "{t}"'}),
 "V-d 编号+state 指南": (lambda: {"guideline":"Each question is 'sN => text', a compressed version of sentence sN. Answer yes only if the text is a grammatical sentence that keeps the main point of sN without distorting it.","sentences":S},
     lambda sid,t:{"type":"noul","instructions":f"{sid} => {t}"}),
 "V-e 拆成两题:语法+忠实": None,
}
SFMT={
 "S-a Score 4级描述+完整说明": (lambda: {"paragraph":para,"sentences":S},
     lambda sid,t:{"type":"score","instructions":f'How much does the span "{t}" in sentence {sid} contribute to the core meaning of the paragraph?',"criteria":["Nothing important: a detail, elaboration or aside","Useful detail a skimming reader can skip","Important: removing it drops a key fact","Essential: the sentence breaks without it"]}, lambda a:a["score"]<1.5),
 "S-b Noul 短说明": (lambda: {"sentences":S},
     lambda sid,t:{"type":"noul","instructions":f'Can "{t}" be deleted from {sid} and a skimming reader still gets its key point?'}, lambda a:a["noul"]>=0.5),
 "S-c Noul 编号+state 指南": (lambda: {"guideline":"Each question quotes a span of sentence sN. Answer yes if the span can be deleted and a skimming reader still gets the key point.","sentences":S},
     lambda sid,t:{"type":"noul","instructions":f'{sid}: "{t}"'}, lambda a:a["noul"]>=0.5),
 "S-d Score 短级别名": (lambda: {"guideline":"Rate how much each quoted span of sentence sN matters to its key point: skip = detail or aside; minor = useful detail; key = key fact; core = sentence breaks without it.","sentences":S},
     lambda sid,t:{"type":"score","instructions":f'{sid}: "{t}"',"criteria":["skip","minor","key","core"]}, lambda a:a["score"]<1.5),
}
def run(fmt_state, fmt_q, items, decide, reps=2):
    accs=[]; toks=[]
    base=post(fmt_state(),{"z":{"type":"noul","instructions":"x"}})[1]
    for _ in range(reps):
        qs={f"q{i}":fmt_q(sid,t) for i,(sid,t,_) in enumerate(items)}
        ans,tk=post(fmt_state(),qs)
        accs.append(sum(decide(ans[f"q{i}"])==bool(y) for i,(_,_,y) in enumerate(items))/len(items)); toks.append(tk)
    per_q=(statistics.mean(toks)-base)/len(items)
    return statistics.mean(accs), per_q, statistics.mean(toks), base
import sys
if len(sys.argv)>1: VFMT={}; SFMT={}
print(f"验证题（{len(VER)} 条）")
for name,f in VFMT.items():
    if f is None:
        # 语法、忠实各一题，两者都 >=0.7 才通过
        accs=[];toks=[]
        st={"sentences":S}; base=post(st,{"z":{"type":"noul","instructions":"x"}})[1]
        for _ in range(2):
            qs={}
            for i,(sid,t,_) in enumerate(VER):
                qs[f"g{i}"]={"type":"noul","instructions":f'Grammatical English sentence: "{t}"'}
                qs[f"f{i}"]={"type":"noul","instructions":f'Keeps the main point of {sid} without distorting it: "{t}"'}
            ans,tk=post(st,qs)
            accs.append(sum(((ans[f"g{i}"]["noul"]>=0.7) and (ans[f"f{i}"]["noul"]>=0.7))==bool(y) for i,(_,_,y) in enumerate(VER))/len(VER)); toks.append(tk)
        print(f"  {name:28s} 准确率 {statistics.mean(accs):5.0%}  每条 {(statistics.mean(toks)-base)/len(VER):5.1f} tok  state基础 {base}")
        continue
    acc,pq,tot,base=run(f[0],f[1],VER,lambda a:a["noul"]>=0.7)
    print(f"  {name:28s} 准确率 {acc:5.0%}  每条 {pq:5.1f} tok  state基础 {base}")
print(f"成分题（{len(SPN)} 条）")
for name,(fs,fq,dec) in SFMT.items():
    acc,pq,tot,base=run(fs,fq,SPN,dec)
    print(f"  {name:28s} 准确率 {acc:5.0%}  每条 {pq:5.1f} tok  state基础 {base}")

print("\n--- 补充：state 只放带编号的连贯段落 ---")
inline=" ".join(f"[{k}] {v}" for k,v in S.items())
EXTRA={
 "V-a 原写法（复测）": (lambda: {"paragraph":para,"sentences":S},
     lambda sid,t:{"type":"noul","instructions":{"task":f"Is the compressed version a grammatical English sentence that stays faithful to sentence {sid} (keeps its main point, no meaning distorted)?","compressed":t}}),
 "V-f 编号段落+完整说明": (lambda: {"paragraph":inline},
     lambda sid,t:{"type":"noul","instructions":{"task":f"Is the compressed version a grammatical English sentence that stays faithful to sentence {sid} (keeps its main point, no meaning distorted)?","compressed":t}}),
 "V-g 编号段落+中等说明": (lambda: {"paragraph":inline},
     lambda sid,t:{"type":"noul","instructions":{"task":f"Grammatical sentence faithful to {sid}?","compressed":t}}),
 "V-h 编号段落+中等说明(纯文本)": (lambda: {"paragraph":inline},
     lambda sid,t:{"type":"noul","instructions":f'Is this a grammatical sentence that stays faithful to {sid}? "{t}"'}),
}
for name,(fs,fq) in EXTRA.items():
    acc,pq,tot,base=run(fs,fq,VER,lambda a:a["noul"]>=0.7,reps=3)
    print(f"  {name:28s} 准确率 {acc:5.0%}  每条 {pq:5.1f} tok  state基础 {base}")
print("\n--- 成分题：编号段落 state ---")
for name,(fs,fq,dec) in {"S-c' 编号段落+指南": (lambda: {"guideline":"Each question quotes a span of sentence sN. Answer yes if the span can be deleted and a skimming reader still gets the key point.","paragraph":inline},
     lambda sid,t:{"type":"noul","instructions":f'{sid}: "{t}"'}, lambda a:a["noul"]>=0.5)}.items():
    acc,pq,tot,base=run(fs,fq,SPN,dec,reps=3)
    print(f"  {name:28s} 准确率 {acc:5.0%}  每条 {pq:5.1f} tok  state基础 {base}")
