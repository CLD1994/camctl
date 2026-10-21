"""START SQL 聚合与完整行消费同一分区，保留启动核实责任。"""
from decimal import Decimal

import pytest

from camctl.contracts.values import new_operation_key,ConsistencyError
from camctl.operations.attempts import AttemptIntent,AttemptTarget,AttemptConfig,OperationKind,QueryPurpose
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.capture_facts import load_start_facts,start_facts_from_rows
from camctl.persistence.transaction import row_facts
from ..scheduling.test_resources import _seed_environment,_seed_plan,_seed_record_action,_seed_activity,_NOW


def save_generic_no_effect_start(owned,action_id,activity_id,occurred_at):
    from camctl.devices.evidence import EvidenceRegistry,EvidenceContract
    from camctl.operations.attempts import AttemptFinish
    from camctl.operations.models import CallOutcome,AttemptStatus,EffectState,ErrorValue,Settlement,SettlementBasis,EvidenceValue
    from camctl.operations.validation import validate_outcome
    repository=OperationRepository()
    begun=repository.begin_attempt(AttemptIntent('control',action_id,OperationKind.START,
        AttemptTarget(activity_id=activity_id),None,AttemptConfig(2,Decimal(10)),occurred_at),new_operation_key(),owned)
    assert begun.kind is DbOutcomeKind.COMPLETED,begun.error
    ticket=begun.value.ticket
    outcome=CallOutcome(status=AttemptStatus.FAILED,effect=EffectState.NO_EFFECT,error=ErrorValue('blocked','dispatch'),
        settlement=Settlement(SettlementBasis.NOT_DISPATCHED,EvidenceValue('dispatch_prevented',1,{})))
    result=repository.finish_attempt(AttemptFinish(ticket,validate_outcome(ticket,outcome,
        EvidenceRegistry((EvidenceContract('dispatch_prevented',1,'control',frozenset()),))),occurred_at),new_operation_key(),owned)
    assert result.kind is DbOutcomeKind.COMPLETED,result.error


@pytest.mark.parametrize('purpose',[None,QueryPurpose.START_CONFIRMATION,QueryPurpose.ACTIVITY_OBSERVATION])
def test_actual_query_responsibility_keeps_sql_and_complete_rows_equivalent(tmp_path,purpose):
    owned=_seed_environment(tmp_path)
    try:
        _seed_plan(owned.connection,1)
        _seed_record_action(owned.connection,11,1)
        _seed_activity(owned.connection,11)
        owned.connection.commit()
        save_generic_no_effect_start(owned,11,11,_NOW)
        if purpose is not None:
            result=OperationRepository().begin_attempt(AttemptIntent('query',11,OperationKind.QUERY_ACTIVITY,
                AttemptTarget(activity_id=11),purpose,AttemptConfig(2,Decimal(10),Decimal(1)),_NOW),new_operation_key(),owned)
            assert result.kind is DbOutcomeKind.COMPLETED,result.error
        action=row_facts(owned.connection,'actions',11)
        activity=row_facts(owned.connection,'device_activities',11)
        runs=[row_facts(owned.connection,'operation_runs',identity) for identity, in owned.connection.execute('SELECT id FROM operation_runs WHERE action_id=11')]
        attempts={run['id']:[row_facts(owned.connection,'operation_attempts',identity) for identity, in
            owned.connection.execute('SELECT id FROM operation_attempts WHERE run_id=?',(run['id'],))] for run in runs}
        sql=load_start_facts(owned.connection,action)
        pure=start_facts_from_rows(action,[activity],runs,attempts)
        assert pure==sql and sql.not_started is (purpose is not QueryPurpose.START_CONFIRMATION)
        if purpose is not None:
            with pytest.raises(ConsistencyError): start_facts_from_rows(action,[activity],runs,{})
            corrupt={key:[{**row,'attempt_no':2} for row in rows] for key,rows in attempts.items()}
            with pytest.raises(ConsistencyError): start_facts_from_rows(action,[activity],runs,corrupt)
    finally:
        owned.connection.close()
