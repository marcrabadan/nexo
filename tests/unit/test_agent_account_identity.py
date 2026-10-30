"""Financial account identity must encode arbitrary model names safely."""


def test_financial_policy_identity_uses_unambiguous_data():
    from nexo.agent_finance import policy_id
    base={'currency':'USD','limit':'1','period':'day'}
    combined={**base,'prices':{'openai:x\t1\t1\nopenai:y':['2','2']}}
    separate={**base,'prices':{'openai:x':['1','1'],'openai:y':['2','2']}}
    assert policy_id(combined)!=policy_id(separate)
