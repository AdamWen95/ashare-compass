"""F3-S uses frozen facts and original formulas, with independent expected values."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
import json
from pathlib import Path

import pytest

from ashare_daily.sector_screening import evaluate_selection, screening_input_hash, validate_screening_config
from ashare_daily.sector_selection import digest


CONFIG=json.loads((Path(__file__).resolve().parents[1]/"config/sector_screening.json").read_text(encoding="utf-8"))
SEEN="2026-09-11T18:00:00+08:00"
CUTOFF="2026-09-11T19:00:00+08:00"


def freeze_selection(selection):
    selection.pop("selection_id",None)
    selection.pop("content_hash",None)
    hashed=digest(selection)
    prefix="validation-sector-" if selection.get("purpose")=="engineering_validation" else "sector-"
    selection.update(content_hash=hashed,selection_id=prefix+selection["target_date"]+"-"+hashed[:20])


def freeze_input(inputs,selection):
    inputs.update(selection_id=selection["selection_id"],selection_content_hash=selection["content_hash"])
    inputs["input_hash"]=screening_input_hash(inputs)


def seal_stock(packet):
    hashes={}
    for row in packet["raw_records"]:
        payload={k:v for k,v in row.items() if k not in {"fact_hash","first_seen_at"}}
        row["fact_hash"]=digest(payload)
        hashes[row["trade_date"]]=row["fact_hash"]
    window=packet.get("adjustment_window")
    if window:
        window["raw_fact_hashes"]=hashes
        body={k:v for k,v in window.items() if k not in {"window_id","content_hash","first_seen_at","observations"}}
        hashed=digest(body)
        window.update(content_hash=hashed,window_id="f2-window-"+hashed)


def fixture(*,count=1,days=120,purpose="production",boards=None,known_risk=False):
    dates=[(date(2025,1,1)+timedelta(days=i)).isoformat() for i in range(days)]
    members,packets=[],{}
    for i in range(count):
        board=(boards or ["sse_main"])[i%len(boards or ["sse_main"])]
        exchange="SSE" if board in {"sse_main","star"} else "SZSE"
        code=str(600000+i) if exchange=="SSE" else str(300000+i)
        security_id="security-"+str(i)
        symbol=("sh." if exchange=="SSE" else "sz.")+code
        member={"security_id":security_id,"code":code,"exchange":exchange,"board":board,"security_type":"ordinary_a",
            "metadata_verified":True,"listing_date":"2020-01-01","listing_status":"listed","sector_ids":["exchange-section:H"],
            "statuses":{key:{"value":False,"effective_date":dates[-1],"evidence_id":"evidence-"+key,"observed_at":SEEN}
                if known_risk else {"value":None,"unknown_reason":"not_reported"} for key in ("st","suspended","delisting_period")}}
        members.append(member)
        rows=[]
        for j,day in enumerate(dates):
            close=Decimal(1000+j)/100
            rows.append({"security_id":security_id,"symbol":symbol,"provider":"sina","trade_date":day,"adjustment_mode":"unadjusted",
                "open":str(close),"high":str(close+Decimal(".1")),"low":str(close-Decimal(".1")),"close":str(close),"preclose":None,
                "volume_shares":1000000,"amount_cny":"100000000","tradestatus":True if known_risk else None,
                "is_st":False if known_risk else None,"price_unit":"CNY","volume_unit":"shares","amount_unit":"CNY",
                "quality_flags":["missing_preclose"],"first_seen_at":SEEN})
        adjusted=[{**{k:v for k,v in r.items() if k!="first_seen_at"},"adjustment_mode":"forward_adjusted"} for r in rows]
        window={"security_id":security_id,"symbol":symbol,"provider":"sina","adjustment_mode":"forward_adjusted",
            "window_start":dates[0],"window_end":dates[-1],"expected_dates":dates,"records":adjusted,
            "anchor_kind":"provider_current_at_fetch","adjustment_anchor_hash":"a"*64,"raw_component_hash":"b"*64,
            "factor_component_hash":"c"*64,"first_seen_at":SEEN,
            "observations":[{"batch_id":"batch-"+str(i),"fetched_at":SEEN,"source_response_path":"fixture/response.json","source_file_hash":"d"*64}]}
        packet={"raw_records":rows,"adjustment_window":window,"expected_dates":dates,"issues":[]}
        seal_stock(packet)
        packets[security_id]=packet
    selection={"schema_version":"f2s1-selection-v1","mode":"offline_test","market_scope":"sse_szse_a","research_mode":"sector_first",
        "purpose":purpose,"production_eligible":purpose=="production","members":members,"selected_security_count":len(members),
        "target_date":dates[-1],"cutoff_at":SEEN,"selection_verified":True,"historical_reconstruction":True}
    if purpose=="engineering_validation":
        selection.update(schema_version="f3s-validation-selection-v1",automatic_selection=False,source_selection_id="sector-origin")
    freeze_selection(selection)
    inputs={"schema_version":"f3s-screening-input-v1","mode":"offline_test","purpose":purpose,"target_date":dates[-1],
        "cutoff_at":CUTOFF,"historical_reconstruction":True,"calendar":{"verified":True,"trading_dates":dates,"issues":[]},
        "securities":packets,"benchmark":{"symbol":"sh.000001","security_type":"index","adjustment_mode":"index_native",
            "price_unit":"index_points","provider":"baostock","fetch_version":"fixture-index-v1","fetched_at":SEEN,"first_seen_at":SEEN,
            "records":[{"symbol":"sh.000001","trade_date":day,"close":"100","price_unit":"index_points","quality_flags":[]} for day in dates],
            "issues":[],"file_refs":[{"path":"fixture/benchmark.json","sha256":"e"*64}]},"file_refs":[]}
    freeze_input(inputs,selection)
    return selection,inputs


def evaluate(selection,inputs):
    freeze_input(inputs,selection)
    return evaluate_selection(selection,inputs,CONFIG)


def test_four_boards_use_same_formula_and_unknown_risk_is_separate():
    selection,inputs=fixture(count=4,boards=["sse_main","szse_main","chinext","star"])
    result=evaluate(selection,inputs)
    assert {r["listing_board"] for r in result["evaluations"]}=={"sse_main","szse_main","chinext","star"}
    assert result["counts"]["technical_pass_count"]==4
    assert result["counts"]["eligibility_pending_count"]==4
    assert result["counts"]["candidate_count"]==result["counts"]["formal_verified_opportunity_count"]==0
    assert all(r["observation_label"]=="技术条件达标、资格待核查" for r in result["evaluations"])
    json.dumps(result,allow_nan=False)


def test_independent_fraction_recalculation_and_off_by_one_includes_target():
    selection,inputs=fixture(days=140)
    result=evaluate(selection,inputs)
    row=result["evaluations"][0]
    metric=row["metrics"]
    # Independent rational arithmetic; none of the production formula functions
    # creates these expected values.
    closes=[Fraction(1000+i,100) for i in range(140)]
    average20=sum(closes[-20:],Fraction())/20
    average60=sum(closes[-60:],Fraction())/60
    ret=closes[-1]/closes[-21]-1
    def decimal(value):
        with localcontext() as context:
            context.prec=40
            return Decimal(value.numerator)/Decimal(value.denominator)
    assert Decimal(metric["ma20"])==decimal(average20)
    assert Decimal(metric["ma60"])==decimal(average60)
    assert abs(Decimal(metric["stock_return_20"])-decimal(ret))<Decimal("1e-38")
    assert metric["relative_return_20"]==metric["stock_return_20"]
    assert Decimal(metric["avg_amount_20_cny"])==Decimal(100000000)
    assert len(row["metric_basis"]["calculation_dates"])==120
    assert row["metric_basis"]["return_dates"]==inputs["calendar"]["trading_dates"][-21:]
    assert row["metric_basis"]["return_unit"]=="ratio"


def test_amount_mean_uses_twenty_dates_including_target_and_exact_gate():
    selection,inputs=fixture()
    packet=inputs["securities"]["security-0"]
    for rows in (packet["raw_records"],packet["adjustment_window"]["records"]):
        for row in rows:row["amount_cny"]="50000000"
        rows[-21]["amount_cny"]="1"
    seal_stock(packet)
    row=evaluate(selection,inputs)["evaluations"][0]
    assert row["metrics"]["avg_amount_20_cny"]=="50000000"
    assert next(c for c in row["technical_conditions"] if c["id"]=="liquidity")["status"]=="pass"
    for rows in (packet["raw_records"],packet["adjustment_window"]["records"]):rows[-1]["amount_cny"]="49999999"
    seal_stock(packet)
    row=evaluate(selection,inputs)["evaluations"][0]
    assert row["technical_status"]=="fail"
    assert Decimal(row["metrics"]["avg_amount_20_cny"])==Decimal("49999999.95")


def test_all_123_members_evaluated_despite_20_candidate_display_limit_and_stable_ties():
    selection,inputs=fixture(count=123,known_risk=True)
    result=evaluate(selection,inputs)
    assert len(result["evaluations"])==123
    assert result["counts"]["technical_pass_count"]==123
    assert result["counts"]["candidate_count"]==20
    ranks=sorted((r["technical_rank"],r["symbol"]) for r in result["evaluations"])
    assert [symbol for _,symbol in ranks]==sorted(r["symbol"] for r in result["evaluations"])
    assert result["counts"]["formal_verified_opportunity_count"]==0


def test_engineering_uses_same_numbers_but_cannot_create_production_candidates():
    production,prod_inputs=fixture(known_risk=True)
    validation,validation_inputs=fixture(known_risk=True,purpose="engineering_validation")
    prod=evaluate(production,prod_inputs)
    val=evaluate(validation,validation_inputs)
    assert prod["evaluations"][0]["metrics"]==val["evaluations"][0]["metrics"]
    assert prod["counts"]["candidate_count"]==1 and val["counts"]["candidate_count"]==0
    assert not val["production_eligible"] and val["purpose"]=="engineering_validation"
    assert val["counts"]["formal_verified_opportunity_count"]==0


def test_empty_production_is_not_applicable_with_na_ratio_and_no_sample_fallback():
    selection,inputs=fixture(count=0)
    inputs["benchmark"]={}
    result=evaluate(selection,inputs)
    assert result["status"]=="not_applicable" and not result["evaluations"]
    assert result["technical_computable_ratio"] is None and result["denominator_zero_display"]=="N/A"
    assert result["benchmark"]["status"]=="not_applicable"
    assert sum(result["counts"]["technical_"+s+"_count"] for s in ("pass","fail","unknown","not_applicable"))==0


@pytest.mark.parametrize("field,value",[("min_history_trading_days",119),("min_avg_amount_cny","49999999"),
    ("ma_short_days",19),("ma_long_days",59),("return_days",19),("amount_days",19),
    ("benchmark_id","sz.399001"),("strategy_version","new-strategy"),("max_candidates",100)])
def test_rule_version_cannot_silently_change_or_relax(field,value):
    config=deepcopy(CONFIG)
    config[field]=value
    with pytest.raises(ValueError):validate_screening_config(config)


def test_four_board_config_cannot_keep_old_mainboard_constraint():
    config=deepcopy(CONFIG)
    config["allowed_boards"]=["sse_main","szse_main"]
    with pytest.raises(ValueError):validate_screening_config(config)


@pytest.mark.parametrize("kind",["input_hash","selection_hash","selection_id","purpose","mode","extra_security"])
def test_frozen_input_identity_purpose_and_hash_cannot_drift(kind):
    selection,inputs=fixture()
    if kind=="input_hash":inputs["input_hash"]="0"*64
    elif kind=="selection_hash":selection["content_hash"]="0"*64
    elif kind=="selection_id":inputs["selection_id"]="other"
    elif kind=="purpose":inputs["purpose"]="engineering_validation"
    elif kind=="mode":inputs["mode"]="research"
    elif kind=="extra_security":inputs["securities"]["outside-stock"]={}
    if kind not in {"input_hash","selection_hash"}:inputs["input_hash"]=screening_input_hash(inputs)
    with pytest.raises(ValueError):evaluate_selection(selection,inputs,CONFIG)


def test_missing_member_packet_still_has_unknown_evaluation():
    selection,inputs=fixture(count=2)
    del inputs["securities"]["security-1"]
    result=evaluate(selection,inputs)
    assert len(result["evaluations"])==2 and result["counts"]["technical_unknown_count"]==1
    assert result["counts"]["stock_count"]==sum(result["counts"]["technical_"+state+"_count"] for state in ("pass","fail","unknown","not_applicable"))


@pytest.mark.parametrize("mutation",["missing","duplicate","zero","wrong_date","wrong_unit","wrong_index"])
def test_benchmark_gaps_do_not_turn_into_zero_relative_return(mutation):
    selection,inputs=fixture()
    benchmark=inputs["benchmark"]
    if mutation=="missing":benchmark["records"].pop(-10)
    elif mutation=="duplicate":benchmark["records"].append(deepcopy(benchmark["records"][-1]))
    elif mutation=="zero":benchmark["records"][-10]["close"]="0"
    elif mutation=="wrong_date":benchmark["records"][-1]["trade_date"]="2027-01-01"
    elif mutation=="wrong_unit":benchmark["price_unit"]="CNY"
    elif mutation=="wrong_index":benchmark["symbol"]="sz.399001"
    row=evaluate(selection,inputs)["evaluations"][0]
    assert row["metrics"]["relative_return_20"] is None
    assert row["technical_status"]=="unknown"
    assert next(c for c in row["technical_conditions"] if c["id"]=="relative_strength")["status"]=="unknown"


@pytest.mark.parametrize("mutation",["missing","hash","raw_version","provider","date","source_time","ohlc"])
def test_adjustment_version_must_be_complete_and_bound_to_frozen_raw(mutation):
    selection,inputs=fixture()
    packet=inputs["securities"]["security-0"]
    window=packet["adjustment_window"]
    if mutation=="missing":packet["adjustment_window"]=None
    elif mutation=="hash":window["content_hash"]="0"*64
    elif mutation=="raw_version":window["raw_fact_hashes"][window["expected_dates"][0]]="0"*64
    elif mutation=="provider":window["provider"]="baostock"
    elif mutation=="date":window["records"].pop(0)
    elif mutation=="source_time":window["observations"][0]["fetched_at"]="2026-09-12T18:00:00+08:00"
    elif mutation=="ohlc":
        window["records"][-1]["high"]="0.1"
        seal_stock(packet)
    row=evaluate(selection,inputs)["evaluations"][0]
    assert row["technical_status"]=="unknown" and not row["adjustment_ready"]
    assert row["metrics"]["ma20"] is None


@pytest.mark.parametrize("mutation",["fact_hash","unit","ohlc","nan","infinite","late_seen","missing_date","duplicate_date"])
def test_raw_quality_defects_are_unknown_even_if_another_numeric_gate_fails(mutation):
    selection,inputs=fixture()
    packet=inputs["securities"]["security-0"]
    raw=packet["raw_records"]
    if mutation=="fact_hash":raw[-1]["fact_hash"]="0"*64
    elif mutation=="unit":raw[-1]["price_unit"]="USD"
    elif mutation=="ohlc":raw[-1]["high"]="0.1"
    elif mutation=="nan":raw[-1]["close"]="NaN"
    elif mutation=="infinite":raw[-1]["amount_cny"]="Infinity"
    elif mutation=="late_seen":raw[-1]["first_seen_at"]="2026-09-12T18:00:00+08:00"
    elif mutation=="missing_date":raw.pop(-1)
    elif mutation=="duplicate_date":raw.append(deepcopy(raw[-1]))
    row=evaluate(selection,inputs)["evaluations"][0]
    assert row["technical_status"]=="unknown" and row["data_issues"]


def test_known_st_failure_does_not_erase_computable_numeric_rules():
    selection,inputs=fixture(known_risk=True)
    selection["members"][0]["statuses"]["st"]["value"]=True
    for rows in (inputs["securities"]["security-0"]["raw_records"],inputs["securities"]["security-0"]["adjustment_window"]["records"]):rows[-1]["is_st"]=True
    seal_stock(inputs["securities"]["security-0"])
    freeze_selection(selection)
    row=evaluate(selection,inputs)["evaluations"][0]
    assert row["technical_status"]=="pass" and row["eligibility_status"]=="fail"


def test_conflicting_target_risk_is_pending_and_not_chosen_selectively():
    selection,inputs=fixture(known_risk=True)
    selection["members"][0]["statuses"]["st"]["value"]=True
    freeze_selection(selection)
    row=evaluate(selection,inputs)["evaluations"][0]
    assert row["technical_status"]=="pass" and row["eligibility_status"]=="pending"
    assert "st:conflicting_risk_status_evidence" in row["risk_gaps"]


def test_confirmed_target_full_halt_is_not_applicable_not_synthetic_price():
    selection,inputs=fixture(known_risk=True)
    packet=inputs["securities"]["security-0"]
    selection["members"][0]["statuses"]["suspended"]["value"]=True
    for rows in (packet["raw_records"],packet["adjustment_window"]["records"]):
        rows[-1].update(tradestatus=False,open=None,high=None,low=None,close=None,amount_cny="0",volume_shares=0)
    seal_stock(packet)
    freeze_selection(selection)
    result=evaluate(selection,inputs)
    row=result["evaluations"][0]
    assert row["technical_status"]=="not_applicable" and row["eligibility_status"]=="fail"
    assert row["metrics"]["adjusted_close"] is None
    assert all(c["status"]=="not_applicable" for c in row["technical_conditions"])


def test_unknown_zero_activity_does_not_become_normal_trading_or_known_suspension():
    selection,inputs=fixture()
    packet=inputs["securities"]["security-0"]
    for rows in (packet["raw_records"],packet["adjustment_window"]["records"]):rows[-1].update(amount_cny="0",volume_shares=0)
    seal_stock(packet)
    row=evaluate(selection,inputs)["evaluations"][0]
    assert row["technical_status"]=="unknown" and row["risk_states"]["suspended"] is None


def test_known_nonordinary_identity_gets_explicit_not_applicable_row():
    selection,inputs=fixture()
    selection["members"][0]["security_type"]="cdr"
    freeze_selection(selection)
    result=evaluate(selection,inputs)
    assert result["counts"]["stock_count"]==1
    assert result["counts"]["technical_not_applicable_count"]==1
    assert result["counts"]["eligibility_fail_count"]==1


def test_fixed_120_window_does_not_use_extra_earlier_cache_rows_to_fill_gaps():
    selection,inputs=fixture(days=320)
    packet=inputs["securities"]["security-0"]
    for rows in (packet["raw_records"],packet["adjustment_window"]["records"]):
        rows[-80]["amount_cny"]=None
        rows[-80]["quality_flags"].append("missing_amount_cny")
    seal_stock(packet)
    row=evaluate(selection,inputs)["evaluations"][0]
    assert row["metrics"]["valid_history_count"]==119
    assert row["technical_status"]=="unknown"
    assert len(row["metric_basis"]["calculation_dates"])==120


def test_same_frozen_inputs_are_deterministic_and_not_modified():
    selection,inputs=fixture()
    original=deepcopy((selection,inputs))
    first=evaluate_selection(selection,inputs,CONFIG)
    second=evaluate_selection(selection,inputs,CONFIG)
    assert first==second and (selection,inputs)==original


def dated_supplement(target):
    return {"value":True,"verified":True,"source":"baostock","full_day":True,
        "as_of_date":target,"effective_from":target,"effective_to":target,
        "source_path":"fixture/status.json","source_file_hash":"a"*64,"source_raw_hash":"b"*64,
        "evidence_id":"frozen-halt-evidence","observed_at":SEEN,"historical_reconstruction":True}


def test_existing_dated_status_supplement_is_retained_with_late_observation():
    selection,inputs=fixture()
    evidence=dated_supplement(selection["target_date"])
    selection["members"][0]["supplemental_status_evidence"]={"suspended":evidence}
    packet=inputs["securities"]["security-0"]
    for rows in (packet["raw_records"],packet["adjustment_window"]["records"]):
        rows[-1].update(tradestatus=False,open=None,high=None,low=None,close=None,amount_cny="0",volume_shares=0)
    seal_stock(packet)
    freeze_selection(selection)
    row=evaluate(selection,inputs)["evaluations"][0]
    assert row["risk_states"]["suspended"] is True
    assert row["technical_status"]=="not_applicable" and row["eligibility_status"]=="fail"
    assert evidence in row["risk_evidence"]["suspended"]


@pytest.mark.parametrize("field,value",[("verified",False),("full_day",False),("as_of_date","2024-01-01"),
    ("effective_from","2027-01-01"),("effective_to","2024-01-01"),("observed_at","2026-09-12T18:00:00+08:00"),
    ("historical_reconstruction",False),("source_file_hash",None)])
def test_unverified_or_misdated_supplement_does_not_make_unknown_status_known(field,value):
    selection,inputs=fixture()
    evidence=dated_supplement(selection["target_date"])
    evidence[field]=value
    selection["members"][0]["supplemental_status_evidence"]={"suspended":evidence}
    freeze_selection(selection)
    row=evaluate(selection,inputs)["evaluations"][0]
    assert row["risk_states"]["suspended"] is None and row["eligibility_status"]=="pending"
    assert row["technical_status"]=="pass"


def test_source_limitations_remain_visible_without_laundering_unknown_risks():
    selection,inputs=fixture()
    packet=inputs["securities"]["security-0"]
    packet["source_limitations"]=["risk_states_unknown","previous_close_may_be_missing","after_hours_inclusion_unverified"]
    row=evaluate(selection,inputs)["evaluations"][0]
    assert row["source_limitations"]==packet["source_limitations"]
    assert row["technical_status"]=="pass" and row["eligibility_status"]=="pending"
    assert row["source_versions"]["raw_providers"]==["sina"]
    assert row["source_versions"]["adjustment_provider"]=="sina"


def partial_fixture():
    selection,inputs=fixture(days=320)
    packet=inputs["securities"]["security-0"]
    missing=packet["expected_dates"][-80]
    packet["raw_records"]=[row for row in packet["raw_records"] if row["trade_date"]!=missing]
    window=packet["adjustment_window"]
    window["records"]=[row for row in window["records"] if row["trade_date"]!=missing]
    window.update(complete=False,diagnostic_only=True,missing_dates=[missing])
    seal_stock(packet)
    packet.update(adjustment_window=None,diagnostic_adjustment_window=window,
        issues=["history_calendar_dates_missing","complete_adjustment_window_missing"])
    return selection,inputs


def test_incomplete_320_diagnostic_retains_recent_windows_without_claiming_readiness():
    selection,inputs=partial_fixture()
    row=evaluate(selection,inputs)["evaluations"][0]
    assert row["technical_status"]=="unknown" and not row["adjustment_ready"]
    assert row["metrics"]["ma20"] is None
    diagnostic=row["diagnostic_metrics"]
    assert diagnostic["complete"] is False and diagnostic["eligible_for_technical_result"] is False
    assert diagnostic["metrics"]["ma20"] is not None and diagnostic["metrics"]["ma60"] is not None
    assert diagnostic["metrics"]["stock_return_20"] is not None and diagnostic["metrics"]["valid_history_count"]==119
    assert len(diagnostic["calculation_dates"])==120 and len(diagnostic["missing_dates"])==1
    assert not diagnostic["issues"] and diagnostic["readiness_issues"]


@pytest.mark.parametrize("mutation",["missing_date_claim","raw_binding","hash","price","hard_error"])
def test_diagnostic_window_still_rejects_invalid_source_versions(mutation):
    selection,inputs=partial_fixture()
    packet=inputs["securities"]["security-0"]
    window=packet["diagnostic_adjustment_window"]
    if mutation=="missing_date_claim":window["missing_dates"]=[]
    elif mutation=="raw_binding":window["raw_fact_hashes"]={}
    elif mutation=="hash":window["content_hash"]="0"*64
    elif mutation=="price":window["records"][-1]["close"]="NaN"
    elif mutation=="hard_error":packet["issues"].append("source_response_identity_conflict")
    row=evaluate(selection,inputs)["evaluations"][0]
    assert row["technical_status"]=="unknown"
    assert row["diagnostic_metrics"]["metrics"]["ma20"] is None
    assert row["diagnostic_metrics"]["issues"]
