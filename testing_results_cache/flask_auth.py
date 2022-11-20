from typing import Optional

import flask_httpauth
from werkzeug import security

from testing_results_cache import flask_db
from testing_results_cache import users

auth = flask_httpauth.HTTPBasicAuth()


@auth.verify_password
def verify_password(username: str, password: str) -> Optional[dict]:
    conn = flask_db.get_db()
    user_id, db_password = users.get_user(conn=conn, user_name=username)
    if user_id == -1:
        return None

    if security.check_password_hash(db_password, password):
        return {"user_id": user_id, "username": username}

    return None
