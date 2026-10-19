"""Versioned market phases, evidence scope and risks; no account instructions."""
from datetime import date

from .contracts import decimal

VERSION = 'premarket-market-state-v1'
LABELS = {'trend_strength':'趋势偏强', 'strong_consolidation':'强势整理', 'mixed_range':'震荡分化',
          'weak_repair':'弱势修复', 'trend_pressure':'趋势承压', 'undetermined':'暂无法判断'}
FOCUS = {
    'trend_strength':'跟踪强势方向的延续性，以及上涨范围和量能是否继续配合。',
    'strong_consolidation':'观察回撤是否有序、原有趋势是否保持，以及强势方向能否重新获得量能支持。',
    'mixed_range':'分开观察指数与个股表现，跟踪相对强势方向，等待相互冲突的信号趋于一致。',
    'weak_repair':'核验近期改善能否连续，是否扩展到更多股票，以及指数能否改善原有弱势结构。',
    'trend_pressure':'跟踪下跌范围与风险是否继续扩散，同时保留有独立依据的相对抗跌观察。',
    'undetermined':'保留已核验事实和个股观察，优先补齐关键市场证据。',
}


def _path(index, day):
    window=index.get('state_window')
    if window is None:
        return None
    if not isinstance(window,dict):
        raise ValueError('market_state_window_invalid')
    dates,raw=window.get('dates'),window.get('closes')
    if (window.get('schema_version')!='premarket-state-window-v1' or not isinstance(dates,list)
            or not isinstance(raw,list) or len(dates)!=21 or len(raw)!=21
            or dates!=sorted(set(dates)) or dates[-1]!=day
            or any(date.fromisoformat(d).isoformat()!=d for d in dates)):
        raise ValueError('market_state_window_invalid')
    closes=[decimal(v,positive=True) for v in raw]
    expected={'close':closes[-1], 'ma20':sum(closes[-20:])/20,
              'previous_ma20':sum(closes[:-1])/20,'return5':closes[-1]/closes[-6]-1}
    if any(abs(decimal(index[k])-v)>decimal('0.000000000001') for k,v in expected.items()):
        raise ValueError('market_state_window_summary_mismatch')
    return {'return1':closes[-1]/closes[-2]-1, 'return3':closes[-1]/closes[-4]-1,
            'up_days3':sum(right>left for left,right in zip(closes[-4:-1],closes[-3:])),
            'drawdown5':closes[-1]/max(closes[-5:])-1}


def _signals(market):
    components=market.get('components',{})
    facts={'breadth':None,'median':None,'close':None,'ma20':None,'previous_ma20':None,
           'return5':None,'activity_ratio':None,'path':None}
    limits=[]
    if components.get('breadth')=='ready':
        try:
            breadth=decimal(market['advancing_fraction'])
            middle=decimal(market['median_change_pct'])
            if not 0<=breadth<=1:
                raise ValueError('breadth_bounds')
            facts.update(breadth=breadth,median=middle)
        except (ValueError,TypeError,KeyError):
            limits.append('全市场广度数值未通过校验。')
    if facts['breadth'] is None:
        limits.append('全市场广度不可用；局部股票样本不用于推断全市场状态。')
    index=market.get('benchmark') or {}
    index_valid=False
    if not isinstance(index,dict):
        index={}
    try:
        day=market.get('market_as_of_date')
        index_valid=date.fromisoformat(day).isoformat()==day and index.get('market_as_of_date')==day
    except (ValueError,TypeError):
        pass
    if components.get('trend')=='ready' and index_valid:
        try:
            values={k:decimal(index[k],positive=k!='return5') for k in
                    ('close','ma20','previous_ma20','return5')}
            if values['return5']<=-1:
                raise ValueError('market_state_return_invalid')
            path=_path(index,market['market_as_of_date'])
            facts.update(**values,path=path)
            if path is None:
                limits.append('最近3个交易日的连续路径未取得，不能确认强势整理或弱势修复。')
        except (ValueError,TypeError,KeyError):
            limits.append('指数价格路径或摘要校验失败，未使用价格趋势证据。')
    if facts['close'] is None:
        limits.append('指数趋势不可用；不推断指数已经转强或转弱。')
    if components.get('activity')=='ready' and index_valid:
        try:
            facts['activity_ratio']=decimal(index['amount'],nonnegative=True)/decimal(index['previous_mean_amount5'],positive=True)
        except (ValueError,TypeError,KeyError):
            pass
    if facts['activity_ratio'] is None:
        limits.append('指数成交活跃度不可用，不能声称已得到量能确认。')
    return facts,limits


def _classify(f):
    b,m,c,ma,old,r5,volume,path=(f[k] for k in
        ('breadth','median','close','ma20','previous_ma20','return5','activity_ratio','path'))
    if c is None and b is None:
        return 'undetermined','没有可用于市场方向判断的已核验指数趋势或全市场广度。'
    if b is not None and b<=decimal('.3') and m<0:
        return 'trend_pressure','全市场上涨占比不高于30%，涨跌幅中位数为负，下跌较广泛。'
    up=c is not None and c>=ma and ma>=old and r5>0
    down=c is not None and c<ma and ma<old and r5<0
    if down and path and path['return3']>0 and path['up_days3']>=2 and path['return1']>0:
        if (b is None or b>=decimal('.5') and m>=0) and (volume is None or volume>=decimal('.8')):
            return 'weak_repair','原有下行结构尚未扭转，但最近3个交易日至少2日上涨且累计回升，现有广度及量能未否定修复。'
    if up and path and (path['return1']<0 or abs(path['return3'])<=decimal('.01')):
        distribution=volume is not None and volume>=decimal('1.2') and path['return1']<0
        if path['drawdown5']>=decimal('-.03') and (b is None or b>=decimal('.4')) and not distribution:
            return 'strong_consolidation','上行结构仍在，近期回撤或窄幅整理，距近5日收盘高点回撤不超过3%，未出现放量下跌组合。'
    if up and (path is None or path['return3']>0 and path['return1']>=0):
        if (b is None or b>=decimal('.6') and m>=0) and (volume is None or volume>=1):
            return 'trend_strength','指数保持上行结构，当前已取得的广度和成交活跃度与偏强判断相符。'
    if c is not None and b is not None and c<ma and b<=decimal('.4') and m<0:
        return 'trend_pressure','指数低于20日均线，同时全市场上涨占比不高于40%、涨跌幅中位数为负。'
    if down and (b is None or b<=decimal('.5') and m<0):
        return 'trend_pressure','指数低于下行的20日均线且5日表现为负，尚无足够的广度证据支持转强。'
    if c is None and b is not None:
        if b>=decimal('.6') and m>=0 and (volume is None or volume>=1):
            return 'trend_strength','仅按已核验的当日全市场广度归为偏强；指数连续趋势仍待核验。'
        if b<=decimal('.4') and m<0:
            return 'trend_pressure','仅按已核验的当日全市场广度归为承压；指数连续趋势仍待核验。'
    return 'mixed_range','指数结构、个股广度或量能信号尚未形成一致方向，存在分化或震荡特征。'


def classify_market_state(market, *, news_ready, events):
    facts,limits=_signals(market)
    state,reason=_classify(facts)
    b,c,volume=facts['breadth'],facts['close'],facts['activity_ratio']
    used=[name for name,value in (('breadth',b),('trend',c),('activity',volume)) if value is not None]
    scope=('whole_market_and_index' if b is not None and c is not None else
           'whole_market_breadth' if b is not None else 'sse_index' if c is not None else
           'observed_sample' if market.get('sample_count',0)>0 else 'none')
    labels={'whole_market_and_index':'全市场广度与上证指数','whole_market_breadth':'仅全市场广度',
            'sse_index':'仅上证指数','observed_sample':'局部股票样本（仅用于方向和股票观察）','none':'关键市场证据不足'}
    if scope=='whole_market_breadth' and volume is not None:
        labels[scope]='全市场广度与指数成交活跃度（指数趋势待核验）'
    if news_ready is not True:
        limits.append('消息覆盖有限，当前状态主要依据已核验量价，不代表已覆盖全部事件。')
    limits.extend(market.get('preparation_limitations',[]))
    analysis=[]
    if b is not None:
        analysis.append(f"全市场上涨占比{b*100:.1f}%，涨跌幅中位数{facts['median']:.2f}%。")
    if c is not None:
        slope='上升' if facts['ma20']>facts['previous_ma20'] else '下降' if facts['ma20']<facts['previous_ma20'] else '持平'
        analysis.append(f"上证指数{c:.2f}，20日均线{facts['ma20']:.2f}（较前一交易日{slope}），5日涨跌幅{facts['return5']*100:.2f}%。")
    if facts['path']:
        p=facts['path']
        analysis.append(f"最近3个交易日累计{p['return3']*100:.2f}%，其中{p['up_days3']}日上涨；距近5日收盘高点{p['drawdown5']*100:.2f}%。")
    if volume is not None:
        analysis.append(f"指数成交额为此前5个交易日均值的{volume*100:.1f}%。")
    risks=[]
    def risk(code,text,severity='attention',event_ids=()):
        risks.append({'code':code,'text':text,'severity':severity,'event_ids':list(event_ids)})
    if c is not None and c<facts['ma20']:
        risk('below_ma20','指数仍低于20日均线，局部回升尚不能视为趋势已经转强。')
    if volume is not None and volume<1:
        risk('activity_below_mean','指数成交额低于此前5日均值，持续性仍需量能验证。')
    if b is not None and b<=decimal('.4') and facts['median']<0:
        risk('broad_decline','下跌覆盖较广，局部强势不能代表整体风险解除。')
    if facts['path'] and facts['path']['return1']<0 and volume is not None and volume>=decimal('1.2'):
        risk('high_volume_decline','出现指数放量下跌组合，需要关注抛压是否延续。','high')
    adverse=[e.get('event_id') for e in events if e.get('validation_status')=='verified'
             and e.get('risk_status')=='adverse' and e.get('impact_scope')=='market']
    if adverse:
        risk('verified_adverse_event','存在已核验的市场不利事件，应结合下方事件依据评估；量价状态不表示风险已消除。','high',adverse)
    pending=[e.get('event_id') for e in events if e.get('validation_status')!='verified' and e.get('impact_scope')=='market']
    if pending:
        risk('unverified_market_event','存在尚待核验的市场事件，未将其当成确定的利好或利空。','pending',pending)
    return {'rule_version':VERSION,'state':state,'label':LABELS[state], 'reasons':[reason],
        'observation_focus':FOCUS[state],'analysis':analysis,
        'evidence_basis':{'scope':scope,'label':labels[scope], 'used_components':used,
            'status':'insufficient' if state=='undetermined' else 'partial' if limits else 'complete',
            'limitations':list(dict.fromkeys(limits))},
        'risk_notes':risks,'market_as_of_date':market.get('market_as_of_date'),
        'validation_status':'heuristic_not_performance_validated',
        'notice':'市场状态用于研究与观察，不对应个人持仓、交易数量或收益预测。'}
