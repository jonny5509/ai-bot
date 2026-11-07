"""Permission helpers. EmlaLock API authorization remains authoritative."""


def user_matches_session(emlalock_user_id: str, session: dict) -> dict:
    wearer = str(session.get("wearerid", "")) == str(emlalock_user_id)
    holder = str(session.get("holderid", "")) == str(emlalock_user_id)
    return {"wearer": wearer, "keyholder": holder, "dual_role": wearer and holder}


def require_authorized_role(*, is_wearer: bool, is_holder: bool, operation: str):
    if operation in {"add", "subtract", "random_add", "random_subtract"} and not (is_wearer or is_holder):
        raise PermissionError("No matching session relationship was established.")
    if operation in {"subtract", "random_subtract"} and not is_holder:
        raise PermissionError("This operation requires holder authorization.")
