"""Exchange-industry partition requires frozen source boundaries, never a list length."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

from ashare_daily.providers.exchange_industry import exchange_industry_snapshot, SECTION_LABELS, TAXONOMY
from ashare_daily.providers.exchange_universe import SSE_ENDPOINT, SZSE_ENDPOINT
from ashare_daily.universe import _json


def dump(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze(root, universe):
    universe.pop("snapshot_id",None)
    universe.pop("content_hash",None)
    value=hashlib.sha256(_json(universe).encode()).hexdigest()
    universe.update(content_hash=value,snapshot_id="universe-2026-09-11-"+value[:20])
    dump(root/"archive/snapshot.json",universe)


def fixture(root):
    raw_sse_main={"A_STOCK_CODE":"600001","COMPANY_CODE":"600001","STOCK_TYPE":"1","LIST_BOARD":"1",
        "CSRC_CODE":"C","CSRC_CODE_DESC":SECTION_LABELS["C"][0]}
    raw_sse_star={"A_STOCK_CODE":"688001","COMPANY_CODE":"688001","STOCK_TYPE":"8","LIST_BOARD":"2",
        "CSRC_CODE":"I","CSRC_CODE_DESC":SECTION_LABELS["I"][0]}
    raw_sse_cdr={**raw_sse_star,"A_STOCK_CODE":"689001","COMPANY_CODE":"689001"}
    raw_szse_main={"agdm":"000001","bk":"主板","sshymc":"J "+SECTION_LABELS["J"][1]}
    raw_chinext={"agdm":"300001","bk":"创业板","sshymc":"C "+SECTION_LABELS["C"][1]}
    defs=[("sse","main_a","SSE","sse_main",[raw_sse_main]),
        ("sse","star","SSE","star",[raw_sse_star,raw_sse_cdr]),
        ("szse","a_shares","SZSE",None,[raw_szse_main,raw_chinext])]
    members,pages,manifests=[],[],[]
    for provider,dataset,exchange,board,rows in defs:
        if provider=="sse":
            params={"STOCK_TYPE":"1" if dataset=="main_a" else "8","REG_PROVINCE":"","CSRC_CODE":"","STOCK_CODE":"",
                "COMPANY_STATUS":"2,4,5,7,8","pageHelp.pageNo":1}
            body={"sqlId":"COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L","result":rows,
                "pageHelp":{"data":rows,"pageNo":1,"pageSize":25,"pageCount":1,"total":len(rows)}}
            endpoint=SSE_ENDPOINT
            business=None
        else:
            params={"SHOWTYPE":"JSON","CATALOGID":"1110","TABKEY":"tab1","PAGENO":1}
            options=[{"value":"","text":"全部行业类别"}]+[{"value":key,"text":key+" "+labels[1]} for key,labels in SECTION_LABELS.items()]
            body=[{"metadata":{"catalogid":"1110","name":"A股列表","tabkey":"tab1","subname":"2026-09-11 ",
                "pageno":1,"pagesize":20,"pagecount":1,"recordcount":len(rows),
                "cols":{"agdm":"A股代码","bk":"板块","sshymc":"所属行业"},
                "conditions":[{"name":"selectHylb","options":options}]},"data":rows}]
            endpoint=SZSE_ENDPOINT
            business="2026-09-11"
        directory=root/"archive/exchange_lists/requests"
        raw_path=directory/(dataset+".raw")
        raw_hash=dump(raw_path,body)
        response={"ok":True,"http_status":200,"verification_kind":"offline_test","provenance_mode":"offline_test",
            "target_date":"2026-09-11","fetched_at":"2026-09-11T18:00:00+08:00","url":endpoint+"?"+urlencode(params),
            "params":params,"body":body,"raw_path":str(raw_path),"raw_sha256":raw_hash}
        response_path=directory/(dataset+".json")
        dump(response_path,response)
        boundary={"page_number":1,"page_size":25 if provider=="sse" else 20,"expected_pages":1,
            "expected_records":len(rows),"terminal":True,"source_as_of_date":business}
        pages.append({"provider":provider,"dataset":dataset,"page_number":1,"record_count":len(rows),"terminal":True,
            "raw_sha256":raw_hash,"raw_response_path":str(response_path),"source_boundary":boundary})
        manifests.append({"provider":provider,"dataset":dataset,"complete":True,"authoritative":True,
            "permission_status":"approved","provenance_mode":"offline_test","expected_pages":1,"expected_records":len(rows),"errors":[]})
        for raw in rows:
            code=raw.get("A_STOCK_CODE") or raw["agdm"]
            members.append({"security_id":"fixture-"+code,"code":code,"exchange":exchange,
                "board":board or {"主板":"szse_main","创业板":"chinext"}[raw["bk"]],
                "security_type":"cdr" if code=="689001" else "ordinary_a","metadata_verified":True,
                "metadata_issues":[],"evidence_id":"fixture-evidence-"+code,
                "raw":{"listing":raw,"detail":{}} if provider=="sse" else raw})
    universe={"mode":"offline_test","scope":"sse_szse_a","universe_verified":True,"collection_ready":True,"blockers":[],
        "requested_date":"2026-09-11","observed_at":"2026-09-11T18:00:01+08:00",
        "members":members,"page_manifest":pages,"source_manifests":manifests}
    freeze(root,universe)
    return universe


def rewrite_page(root, universe, group, mutate):
    page=next(p for p in universe["page_manifest"] if p["dataset"]==group)
    response_path=Path(page["raw_response_path"])
    response=json.loads(response_path.read_text(encoding="utf-8"))
    mutate(response)
    page["raw_sha256"]=response["raw_sha256"]=dump(Path(response["raw_path"]),response["body"])
    dump(response_path,response)
    freeze(root,universe)


def test_complete_partition_keeps_cdr_and_all_listing_boards(tmp_path):
    universe=fixture(tmp_path)
    result=exchange_industry_snapshot(tmp_path,universe)
    catalog=result["catalog"]
    assert catalog["complete"] and catalog["boundary_verified"]
    assert catalog["taxonomy"]==TAXONOMY and len(catalog["rows"])==19
    assert catalog["raw_discovered_members"]==5 and catalog["unmapped_count"]==0
    assert catalog["source_member_type_counts"]=={"ordinary_a":4,"cdr":1}
    rows=[row for value in result["memberships"].values() for row in value["rows"]]
    assert {r["listing_board"] for r in rows}=={"sse_main","szse_main","chinext","star"}
    assert len({r["security_id"] for r in rows})==5
    assert len(catalog["file_refs"])==7 and catalog["evidence"]==[]
    assert catalog["source_official_revision"] is None
    assert catalog["new_network_requests"]==0 and catalog["provenance_mode"]=="offline_test"


def test_zero_member_section_is_complete_empty_and_dates_remain_source_specific(tmp_path):
    result=exchange_industry_snapshot(tmp_path,fixture(tmp_path))
    assert result["memberships"]["exchange-section:O"]["complete"]
    assert result["memberships"]["exchange-section:O"]["zero_members"]
    for member in (r for v in result["memberships"].values() for r in v["rows"]):
        assert member["source_business_date"]==(None if member["exchange"]=="SSE" else "2026-09-11")
    assert result["catalog"]["source_business_date"] is None


def test_mapping_is_explicit_and_does_not_claim_sse_unobserved_o_label(tmp_path):
    result=exchange_industry_snapshot(tmp_path,fixture(tmp_path))
    rows={r["code"]:r for r in result["catalog"]["normalization_mapping"]}
    assert rows["D"]["source_aliases"]=={"SSE":"电力、热力、燃气及水生产和供应业","SZSE":"水电煤气"}
    assert rows["O"]["source_aliases"]["SSE"] is None
    assert all(r["source_official_revision"] is None for r in rows.values())


@pytest.mark.parametrize("change",[lambda u:u.update(universe_verified=False),lambda u:u.update(collection_ready=False),
    lambda u:u.update(scope="all_a"),lambda u:u.update(mode="demo"),lambda u:u.update(blockers=["missing"]),
    lambda u:u.update(content_hash="0"*64)])
def test_unverified_universe_cannot_certify_partition(tmp_path,change):
    universe=fixture(tmp_path)
    change(universe)
    result=exchange_industry_snapshot(tmp_path,universe)
    assert not result["catalog"]["complete"] and result["catalog"]["issues"]


def test_original_universe_file_required_and_not_mutated(tmp_path):
    universe=fixture(tmp_path)
    path=tmp_path/"archive/snapshot.json"
    original=path.read_bytes()
    result=exchange_industry_snapshot(tmp_path,universe)
    assert path.read_bytes()==original and result["catalog"]["complete"]
    path.unlink()
    assert not exchange_industry_snapshot(tmp_path,universe)["catalog"]["complete"]


@pytest.mark.parametrize("mutation",["raw_missing","raw_tamper","body_tamper","http_error","mode_spoof","url_wrong","filtered"])
def test_every_source_page_must_reconcile_immutable_raw_and_request(tmp_path,mutation):
    universe=fixture(tmp_path)
    path=Path(universe["page_manifest"][0]["raw_response_path"])
    value=json.loads(path.read_text(encoding="utf-8"))
    if mutation=="raw_missing": Path(value["raw_path"]).unlink()
    elif mutation=="raw_tamper": Path(value["raw_path"]).write_text("{}",encoding="utf-8")
    elif mutation=="body_tamper": value["body"]={}
    elif mutation=="http_error": value["http_status"]=403
    elif mutation=="mode_spoof": value.update(verification_kind="live_network",provenance_mode="online")
    elif mutation=="url_wrong": value["url"]=value["url"].replace("query.sse.com.cn","example.com")
    elif mutation=="filtered":
        value["params"]["CSRC_CODE"]="C"
        value["url"]=SSE_ENDPOINT+"?"+urlencode(value["params"])
    dump(path,value)
    result=exchange_industry_snapshot(tmp_path,universe)
    assert not result["catalog"]["complete"] and result["catalog"]["issues"]


@pytest.mark.parametrize("mutation",["missing","duplicate","false_terminal","wrong_total"])
def test_source_boundaries_cannot_be_replaced_with_len(tmp_path,mutation):
    universe=fixture(tmp_path)
    if mutation=="missing": universe["page_manifest"].pop()
    elif mutation=="duplicate": universe["page_manifest"].append(deepcopy(universe["page_manifest"][0]))
    elif mutation=="false_terminal": universe["page_manifest"][0]["terminal"]=False
    elif mutation=="wrong_total": universe["source_manifests"][0]["expected_records"]=100
    freeze(tmp_path,universe)
    assert not exchange_industry_snapshot(tmp_path,universe)["catalog"]["complete"]


def test_source_industry_dictionary_change_blocks(tmp_path):
    universe=fixture(tmp_path)
    def mutate(response):
        response["body"][0]["metadata"]["conditions"][0]["options"][1]["text"]="A 新含义"
    rewrite_page(tmp_path,universe,"a_shares",mutate)
    result=exchange_industry_snapshot(tmp_path,universe)
    assert "exchange_szse_industry_enum_changed" in result["catalog"]["issues"]


def test_source_member_omission_is_not_hidden_by_rehashed_snapshot(tmp_path):
    universe=fixture(tmp_path)
    universe["members"].pop()
    freeze(tmp_path,universe)
    result=exchange_industry_snapshot(tmp_path,universe)
    assert "industry_universe_omits_source_members" in result["catalog"]["issues"]


def test_member_industry_tamper_fails_original_raw_comparison(tmp_path):
    universe=fixture(tmp_path)
    universe["members"][0]["raw"]["listing"]["CSRC_CODE"]="J"
    freeze(tmp_path,universe)
    result=exchange_industry_snapshot(tmp_path,universe)
    assert "industry_member_raw_source_conflict" in result["catalog"]["issues"]


def test_duplicate_security_identity_is_not_folded_into_one_member(tmp_path):
    universe=fixture(tmp_path)
    universe["members"][1]["security_id"]=universe["members"][0]["security_id"]
    freeze(tmp_path,universe)
    assert "industry_security_id_missing_or_conflicting" in exchange_industry_snapshot(tmp_path,universe)["catalog"]["issues"]


def test_unknown_source_label_is_not_guessed_from_code(tmp_path):
    universe=fixture(tmp_path)
    universe["members"][0]["raw"]["listing"]["CSRC_CODE_DESC"]="未知行业名称"
    def mutate(response):
        response["body"]["result"][0]["CSRC_CODE_DESC"]="未知行业名称"
        response["body"]["pageHelp"]["data"][0]["CSRC_CODE_DESC"]="未知行业名称"
    rewrite_page(tmp_path,universe,"main_a",mutate)
    result=exchange_industry_snapshot(tmp_path,universe)
    assert not result["catalog"]["complete"] and result["catalog"]["unmapped_count"]==1
    assert any(issue.startswith("industry_code_or_label_unmapped") for issue in result["catalog"]["issues"])


def test_source_offline_evidence_cannot_enter_research_universe(tmp_path):
    universe=fixture(tmp_path)
    universe["mode"]="research"
    for manifest in universe["source_manifests"]: manifest["provenance_mode"]="online"
    freeze(tmp_path,universe)
    assert "exchange_http_response_provenance_invalid" in exchange_industry_snapshot(tmp_path,universe)["catalog"]["issues"]


def test_different_frozen_universe_rejected_even_with_valid_content_hash(tmp_path):
    universe=fixture(tmp_path)
    original=deepcopy(universe)
    universe["observed_at"]="2026-09-11T19:00:01+08:00"
    freeze(tmp_path,universe)
    assert "original_universe_snapshot_differs" in exchange_industry_snapshot(tmp_path,original)["catalog"]["issues"]
