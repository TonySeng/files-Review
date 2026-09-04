"""缓存失效正确性验证：文件 MD5 或规则内容变更必须让缓存键漂移，避免返回陈旧结论。

运行：python tests/test_cache_invalidation.py  （需在项目根目录，backend 为包）
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from services import findings_cache, consistency_cache, versioning  # noqa: E402


def check(name: str, cond: bool) -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"验证失败: {name}")


def _rule() -> dict:
    return {
        "id": "cons-cross",
        "name": "跨文件信息一致性",
        "category": "consistency",
        "severity": "major",
        "description": "同一信息在多处是否矛盾",
        "checkpoints": ["投标人名称一致", "报价金额一致"],
        "need_legal_basis": False,
        "structured": {
            "consistency_elements": [
                {"name": "投标人名称", "synonyms": ["供应商名称"], "note": "须与公章一致"}
            ]
        },
    }


def test_rule_content_hash_sensitive():
    r = _rule()
    h1 = versioning.rule_content_hash(r)
    # 改 checkpoints → 指纹必须变
    r2 = _rule()
    r2["checkpoints"] = ["投标人名称一致", "报价金额一致", "新增一条"]
    h2 = versioning.rule_content_hash(r2)
    check("改 checkpoints 后规则内容指纹变化", h1 != h2)
    # 改 description → 指纹必须变
    r3 = _rule()
    r3["description"] = "改了描述"
    check("改 description 后规则内容指纹变化", versioning.rule_content_hash(r3) != h1)
    # 改 consistency_elements → 指纹必须变
    r4 = _rule()
    r4["structured"]["consistency_elements"][0]["note"] = "比对要求变了"
    check("改一致性要素比对要求后指纹变化", versioning.rule_content_hash(r4) != h1)
    # ID 不变、内容不变 → 指纹不变（同一规则）
    check("相同内容指纹稳定", versioning.rule_content_hash(_rule()) == h1)


def test_findings_cache_key_includes_md5_and_rule_fp():
    base = dict(
        doc_hashes=["h1"],
        mode="bid",
        kb_enabled=False,
        web_search_enabled=False,
        det_mode=True,
        tender_summary="",
        global_kb_context="",
        extra_instruction="",
    )
    k0 = findings_cache.make_key(**base, doc_md5s=["md5-aaa"], rule_fps=["fp-xxx"], rule_ids=["cons-cross"])
    # 仅文件 MD5 变 → 键必须变
    k_md5 = findings_cache.make_key(**base, doc_md5s=["md5-bbb"], rule_fps=["fp-xxx"], rule_ids=["cons-cross"])
    check("文件 MD5 变化使结论缓存键变化", k_md5 != k0)
    # 仅规则指纹变（同一 ID）→ 键必须变
    k_fp = findings_cache.make_key(**base, doc_md5s=["md5-aaa"], rule_fps=["fp-yyy"], rule_ids=["cons-cross"])
    check("规则内容指纹变化（同 ID）使结论缓存键变化", k_fp != k0)
    # 规则 ID 变 → 键变（保持）
    k_id = findings_cache.make_key(**base, doc_md5s=["md5-aaa"], rule_fps=["fp-xxx"], rule_ids=["cons-tender-match"])
    check("规则 ID 变化使结论缓存键变化", k_id != k0)


def test_consistency_cache_key_includes_md5_and_rule_token():
    base = dict(
        doc_text="相同文本",
        mode="bid",
        elements=[{"name": "投标人名称", "synonyms": ["供应商名称"], "note": ""}],
        temperature=0.0,
        kind="summary",
    )
    k0 = consistency_cache.make_key(**base, doc_md5="md5-aaa", rule_token="tok-1")
    # 文件 MD5 变 → 键变
    k_md5 = consistency_cache.make_key(**base, doc_md5="md5-bbb", rule_token="tok-1")
    check("文件 MD5 变化使一致性缓存键变化", k_md5 != k0)
    # 一致性规则 token 变（规则定义改）→ 键变
    k_tok = consistency_cache.make_key(**base, doc_md5="md5-aaa", rule_token="tok-2")
    check("一致性规则 token 变化使缓存键变化", k_tok != k0)


def test_consistency_rule_token_changes_with_rule_edit():
    rules = [_rule()]
    tok1 = versioning.consistency_rule_token(rules)
    r2 = _rule()
    r2["checkpoints"] = r2["checkpoints"] + ["又一条"]
    tok2 = versioning.consistency_rule_token([r2])
    check("一致性规则内容变更后 token 变化", tok1 != tok2)
    # 非一致性规则不影响一致性 token
    other = {"id": "qual-1", "name": "资质", "category": "qualification",
             "checkpoints": ["x"], "description": "d"}
    check("非一致性规则不计入一致性 token",
          versioning.consistency_rule_token([_rule(), other]) == tok1)


if __name__ == "__main__":
    test_rule_content_hash_sensitive()
    test_findings_cache_key_includes_md5_and_rule_fp()
    test_consistency_cache_key_includes_md5_and_rule_token()
    test_consistency_rule_token_changes_with_rule_edit()
    print("\nALL CACHE-INVALIDATION CHECKS PASSED")
