#!/usr/bin/env python3
from pathlib import Path

path = Path("backend/main.py")
text = path.read_text(encoding="utf-8")
old = '''# ---------- Aufgaben (tasks) ----------
@app.get("/api/objects/{object_id}/tasks")
def get_tasks(object_id: str, user: dict = Depends(get_current_user), _: None = Depends(require_object_access)):
    o = _load_repo_objekte_lib()
    return {"tasks": o.list_tasks(object_id)}


class TaskBody(BaseModel):
    text: str


@app.post("/api/objects/{object_id}/tasks")
def create_task(object_id: str, body: TaskBody, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    o = _load_repo_objekte_lib()
    if not body.text.strip():
        raise HTTPException(400, "Текст не может быть пустым")
    task_id = o.add_task(object_id, body.text.strip(), user.get('first_name', str(user['id'])))
    return {"task_id": task_id}


@app.patch("/api/tasks/{task_id}/complete")
def complete_task(task_id: str, user: dict = Depends(get_current_user), _: None = Depends(require_owner)):
    o = _load_repo_objekte_lib()
    try:
        o.complete_task(task_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"status": "ok"}


'''
new = '''# ---------- Tasks domain ----------
if __package__:
    from .routes.tasks import TaskBody, TasksRouteDeps, create_tasks_router
else:
    from routes.tasks import TaskBody, TasksRouteDeps, create_tasks_router  # noqa: E402

_tasks_router, _tasks_handlers = create_tasks_router(
    TasksRouteDeps(
        get_current_user=get_current_user,
        require_owner=require_owner,
        require_object_access=require_object_access,
        load_objekte_lib=lambda: _load_repo_objekte_lib(),
    )
)
app.include_router(_tasks_router)

# Temporary direct-call compatibility while main.py is decomposed. These are
# aliases only; all task implementation lives in routes/tasks.py.
get_tasks = _tasks_handlers.get_tasks
create_task = _tasks_handlers.create_task
complete_task = _tasks_handlers.complete_task


'''
if text.count(old) != 1:
    raise SystemExit(f"expected exactly one legacy tasks block, found {text.count(old)}")
path.write_text(text.replace(old, new), encoding="utf-8")
print("phase4 tasks transform applied")
