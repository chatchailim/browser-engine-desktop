"""Shared fail-closed result contract for runner and command entrypoint."""
import hashlib
import re

def validate_results(results, run_id, targeted=False):
    try:
        return _validate(results, run_id, targeted)
    except (TypeError, ValueError, KeyError, AttributeError, ImportError, OverflowError):
        return False, "Malformed or unverifiable evidence"

def _validate(r, run_id, targeted):
    import rfc8785
    from tests.rehearsal_runner import evaluate_round_e_status, evaluate_overall_rehearsal
    if not isinstance(r, dict) or r.get('overall_ok') is not True or r.get('run_id') != run_id:
        return False, 'Invalid success flag or run identity'
    if not targeted:
        ok = r.get('overall_status') == 'PASS' and evaluate_overall_rehearsal(r) == ('PASS', True)
        return ok, 'Full rehearsal evidence evaluation'
    if (r.get('overall_status') != 'TARGETED_ROUND_E_COMPLETED'
            or r.get('mode') != 'targeted_round_e' or r.get('targeted_round') != 'round_e'):
        return False, 'Targeted mode/status mismatch'
    task = 'rehearsal_e_' + run_id
    e = r['round_e']; a = e['audit']; m = r['manifest']
    if e.get('task_id') != task or e.get('status') != 'completed':
        return False, 'Round identity mismatch'
    if evaluate_round_e_status(a) != ('completed', True):
        return False, 'Round E audit failed'
    proof = a.get('ownership_proof')
    if not isinstance(proof, dict) or proof.get('verified') is not True:
        return False, 'Missing ownership proof'
    if a.get('tree_nonempty') is not True or type(a.get('element_count')) is not int or a['element_count'] < 2:
        return False, 'Empty content evidence'
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', a.get('structure_hash', '')):
        return False, 'Missing structure hash'
    before = a.get('config_copy_pre_hash')
    if not isinstance(before, str) or not re.fullmatch(r'[0-9a-f]{64}', before) or a.get('config_copy_post_hash') != before:
        return False, 'Config restoration hashes mismatch'
    if a.get('cleanup_audit', {}).get('verified') is not True:
        return False, 'Missing cleanup proof'
    if m.get('run_id') != run_id or m.get('mode') != 'targeted_round_e' or m.get('rounds') != {'round_e': {'task_id': task}}:
        return False, 'Manifest identity mismatch'
    facts = r.get('facts')
    if not isinstance(facts, list) or len(facts) != 1:
        return False, 'Targeted run requires exactly one desktop fact'
    f = facts[0]; doc = f['fact_json']; payload = doc['payload']; fid = doc['factId']
    if not isinstance(fid, str) or not fid or f.get('factId') != fid:
        return False, 'Fact ID mismatch'
    if f.get('factType') != 'FactDesktopWindowRead' or doc.get('factType') != 'FactDesktopWindowRead':
        return False, 'Wrong fact type'
    if payload.get('taskId') != task or doc.get('subjectId') != 'desktop-task:' + task:
        return False, 'Fact task mismatch'
    if (type(payload.get('elementCount')) is not int or payload['elementCount'] != a['element_count']
            or payload.get('structureHash') != a['structure_hash']
            or payload.get('windowTitleHash') != a.get('window_title_hash')
            or payload.get('processName') != 'notepad.exe'):
        return False, 'Fact contradicts desktop audit'
    calc = 'sha256:' + hashlib.sha256(rfc8785.dumps({k:v for k,v in doc.items() if k != 'contentHash'})).hexdigest()
    if doc.get('hashAlgorithm') != 'jcs-sha256-v1' or doc.get('contentHash') != calc or f.get('contentHash') != calc or f.get('hash_valid') is not True:
        return False, 'Fact canonical hash invalid'
    if m.get('facts') != {fid: {'fact_type':'FactDesktopWindowRead','content_hash':calc,'valid':True}}:
        return False, 'Manifest facts mismatch'
    return True, 'Targeted evidence verified'
