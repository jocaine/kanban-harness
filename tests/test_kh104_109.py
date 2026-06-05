"""Tests for KH-104 through KH-109 implementation."""

import inspect
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

errors = []

# ==================== KH-104: Workflow Config ====================

# Test 1: workflow_config loads and has correct defaults
try:
    from core.workflow_config import workflow_config
    assert workflow_config.poll_interval == 30, f'poll_interval={workflow_config.poll_interval}'
    assert workflow_config.max_concurrent_sessions == 5
    assert workflow_config.max_research_rounds == 10
    assert workflow_config.stuck_cooldown == 30
    assert workflow_config.default_timeout == 600
    assert workflow_config.stall_timeout == 120
    print('✓ KH-104: workflow_config defaults correct')
except Exception as e:
    errors.append(f'KH-104 defaults: {e}')
    print(f'✗ KH-104: {e}')

# Test 2: hot-reload detects file change
try:
    from core.workflow_config import WorkflowConfig, _CONFIG_PATH
    original = _CONFIG_PATH.read_text()
    modified = original.replace('interval_seconds: 30', 'interval_seconds: 15')
    _CONFIG_PATH.write_text(modified)
    time.sleep(0.01)

    cfg = WorkflowConfig()
    cfg._load()
    assert cfg.poll_interval == 15, f'Expected 15, got {cfg.poll_interval}'
    print('✓ KH-104: hot-reload picks up changed value')

    # Test invalid yaml keeps old config
    _CONFIG_PATH.write_text('invalid: [yaml: {{broken')
    time.sleep(0.01)
    cfg._last_mtime = 0
    cfg.reload_if_changed()
    assert cfg.poll_interval == 15, 'Should keep old value on parse failure'
    print('✓ KH-104: invalid yaml keeps old config')

    _CONFIG_PATH.write_text(original)
except Exception as e:
    errors.append(f'KH-104 reload: {e}')
    print(f'✗ KH-104 reload: {e}')
    try:
        _CONFIG_PATH.write_text(original)
    except Exception:
        pass

# ==================== KH-105: Engine Split ====================

# Test 3: engine has scheduling methods only
try:
    from scheduler.engine import SchedulerEngine
    eng = SchedulerEngine()
    status = eng.status
    assert status['poll_interval'] == 30
    assert status['mode'] == 'stopped'
    assert hasattr(eng, '_tick')
    assert hasattr(eng, '_reconcile_running_sessions')
    assert hasattr(eng, '_find_stuck_cards')
    # Should NOT have handler methods
    assert not hasattr(eng, '_parse_pm_research_decision')
    assert not hasattr(eng, '_run_comment_agent')
    assert not hasattr(eng, '_append_research_to_memory')
    print('✓ KH-105: engine has scheduling methods only, no handler methods')
except Exception as e:
    errors.append(f'KH-105 engine: {e}')
    print(f'✗ KH-105: {e}')

# Test 4: handlers module has all expected functions
try:
    from scheduler import handlers
    assert callable(handlers.handle_coach_dev_result)
    assert callable(handlers.handle_comment_agent_result)
    assert callable(handlers.handle_pm_tool_mode)
    assert callable(handlers.parse_pm_research_decision)
    assert callable(handlers.parse_pm_research_conclusion)
    assert callable(handlers.parse_industry_decision)
    assert callable(handlers.next_status_for_role)
    assert callable(handlers.append_research_to_memory)
    print('✓ KH-105: handlers module has all expected functions')
except Exception as e:
    errors.append(f'KH-105 handlers: {e}')
    print(f'✗ KH-105 handlers: {e}')

# Test 5: parse functions work correctly
try:
    from scheduler.handlers import (
        parse_pm_research_decision, parse_industry_decision,
        next_status_for_role, parse_pm_research_conclusion,
    )

    assert parse_pm_research_decision('[调研充分] 材料完整', 3, 'research') == 'done'
    assert parse_pm_research_decision('[调研充分] OK', 2, 'dev') == 'dev'
    assert parse_pm_research_decision('[需要补充] 缺少数据', 2, 'dev') == 'research'
    assert parse_pm_research_decision('no signal', 0, 'dev') == 'research'
    assert parse_pm_research_decision('no signal', 3, 'dev') == ''
    assert parse_pm_research_decision('no signal', 10, 'dev') == 'dev'  # max rounds

    assert parse_industry_decision('[转给PM] 调研完成') == 'organizing'
    assert parse_industry_decision('[需要补充] 需要CEO确认') == 'research'
    assert parse_industry_decision('继续工作中') == 'research'

    assert next_status_for_role('pm', 'organizing') == ''
    assert next_status_for_role('industry', 'research') == 'organizing'
    assert next_status_for_role('coach_review', 'testing') == ''

    comment = '''[调研充分]
可靠性：高（官方文档）
提炼结论：
- ROS2 支持 Python 和 C++
- 推荐使用 colcon 构建
归档建议：建议写入技术栈文档'''
    parsed = parse_pm_research_conclusion(comment)
    assert parsed is not None
    assert parsed['reliability'] == '高（官方文档）'
    assert len(parsed['conclusions']) == 2
    assert '建议写入技术栈文档' in parsed['archive_target']

    print('✓ KH-105: parse functions produce correct results')
except Exception as e:
    errors.append(f'KH-105 parse: {e}')
    print(f'✗ KH-105 parse: {e}')

# ==================== KH-106: task_done Signal ====================

# Test 6: task_done field in agent returns
try:
    from agents.coach_dev import CoachDev
    src = inspect.getsource(CoachDev.execute)
    assert 'task_done' in src, 'coach_dev.execute should return task_done'

    from agents.comment_agent import CommentAgent
    src2 = inspect.getsource(CommentAgent.execute)
    assert 'task_done' in src2, 'comment_agent.execute should return taske'
    print('✓ KH-106: agents return task_done field')
except Exception as e:
    errors.append(f'KH-106: {e}')
    print(f'✗ KH-106: {e}')

# Test 7: handlers use task_done
try:
    src = inspect.getsource(handlers.handle_coach_dev_result)
    assert 'task_done' in src, 'handler should check task_done'
    print('✓ KH-106: handlers check task_done field')
except Exception as e:
    errors.append(f'KH-106 handler: {e}')
    print(f'✗ KH-106 handler: {e}')

# ==================== KH-107: Debug Endpoint ====================

# Test dpoint exists in router
try:
    from web.api import router
    paths = [r.path for r in router.routes if hasattr(r, 'path')]
    assert '/cards/{code}/debug' in paths, f'debug endpoint not found'
    print('✓ KH-107: /cards/{code}/debug endpoint registered')
except Exception as e:
    errors.append(f'KH-107: {e}')
    print(f'✗ KH-107: {e}')

# ==================== KH-108: Token Stats ====================

# Test 9: token stats endpoint exists
try:
    assert '/stats/tokens' in paths, f'stats/tokens not found'
    print('✓ KH-108: /stats/tokens endpoint registered')
except Exception as e:
    errors.append(f'KH-108: {e}')
    print(f'✗ KH-108: {e}')

# Test 10: sager.complete_session accepts tokens param
try:
    from core.session_manager import SessionManager
    sig = inspect.signature(SessionManager.complete_session)
    assert 'tokens' in sig.parameters, f'tokens param not in {list(sig.parameters)}'
    print('✓ KH-108: SessionManager.complete_session accepts tokens param')
except Exception as e:
    errors.append(f'KH-108 session: {e}')
    print(f'✗ KH-108 session: {e}')

# Test 11: DB migration includes token columns
try:
    from core.database import _migrate_db
    src = inspect.getsource(_migrate_db)
    assert 'input_tokens' in src
    assert 'output_tokens' in src
    assert 'total_tokens' in src
    print('✓ KH-108: DB migration includes token columns')
except Exception as e:
    errors.append(f'KH-108 migration: {e}')
    print(f'✗ KH-108 migration: {e}')

# ==================== KH-109: Security Hardening ====================

# Test 12: path traversal protection
try:
    from core.workspace import validate_path_within_workspace, WORKSPACE_BASE
    os.makedirs(WORKSPACE_BASE, exist_ok=True)

    valid = os.path.join(WORKSPACE_BASE, 'project_1')
    os.makedirs(valid, exist_ok=True)
    result = validate_path_within_workspace(valid)
    assert result == os.path.realpath(valid)

    try:
        validate_path_within_workspace(os.path.join(WORKSPACE_BASE, '..', '..', 'etc', 'passwd'))
        errors.append('KH-109: traversal not blocked')
        print('✗ KH-109: path traversal NOT blocked')
    except ValueError:
        print('✓ KH-109: path traversal correctly blocked')
except Exception as e:
    errors.append(f'KH-109 path: {e}')
    print(f'✗ KH-109 path: {e}')

# Test 13: secret filter
try:
    from core.secret_filter import SecretFilter

    f = SecretFilter()
    record = logging.LogRecord('test', logging.INFO, '', 0,
                               'key is sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456', None, None)
    f.filter(record)
    assert 'sk-ant-' not in record.msg, f'Secret not redacted: {record.msg}'
    assert '***' in record.msg
    print('✓ KH-109: SecretFilter redacts API key patterns')
except Exception as e:
    errors.append(f'KH-109 filter: {e}')
    print(f'✗ KH-109 filter: {e}')

# Test 14: coach_dev has cwd validation
try:
    from agents.coach_dev import CoachDev
    src = inspect.getsource(CoachDev.execute)
    assert 'realpath' in src or 'validate_path' in src or 'outside workspace' in src
    print('✓ KH-109: coach_dev validates cwd before launch')
except Exception as e:
    errors.append(f'KH-109 cwd: {e}')
    print(f'✗ KH-109 cwd: {e}')

# ==================== Summary ====================

print()
if errors:
    print(f'FAILED: {len(errors)} test(s)')
    for e in errors:
        print(f'  - {e}')
    sys.exit(1)
else:
    print('ALL 14 TESTS PASSED ✓')
