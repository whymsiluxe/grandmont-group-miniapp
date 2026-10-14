#!/bin/bash
# BACKEND RUNTIME ARTIFACT MANIFEST — single source of truth.
# Sourced by deploy.sh and rollback.sh (bash arrays).
# Parsed by tests/test_deploy_manifest.py (Python regex).
#
# Rule: every file that main.py imports from backend/ must appear here.
# If you add a new backend lib imported by main.py, add it here and re-run
# the full test suite — it will catch a missing-from-manifest regression.
#
# 17.09: create_object.py/create_object_folder.py MOVED into backend/ (were
# external at /home/promonta/agent/, see docs/OPEN_QUESTIONS_11sep2026.md Q1)
# -- listed in BACKEND_SUBPROCESS_SCRIPTS below, not BACKEND_PY_LIBS, since
# main.py shells out to them (subprocess) rather than importing them.
BACKEND_PY_LIBS=(
    "tools_lib.py"
    "mangel_lib.py"
    "objekte_lib.py"
    "roadmap_lib.py"
    "work_types.py"
    "profile_skills.py"
    "assignment_matching.py"
    "daily_plan_lib.py"
    "system_status.py"
)
BACKEND_JS_FILES=(
    "angebot_free.js"
    "rechnung.js"
)
BACKEND_SUBPROCESS_SCRIPTS=(
    "create_object.py"
    "create_object_folder.py"
)
# 18.09 (audit finding): standalone scripts run only by a systemd timer, NOT
# imported by main.py at all (so they don't belong in BACKEND_PY_LIBS -- that
# list's whole contract is "main.py imports this, keep backup/restore/syntax-
# check transactional with it"). Before this pass they were deployed by a
# one-off manual `cp` into the runtime dir, so a later repo edit would pass CI
# green and get pushed to main while the LIVE systemd timer kept running the
# stale pre-edit copy forever -- classic configuration drift, the repo not
# being the actual source of truth for what's running. deploy_standalone_scripts()
# in runtime_manifest.sh now copies this list into the runtime dir on every
# deploy, same as every other deploy artifact.
BACKEND_STANDALONE_SCRIPTS=(
    "cleanup_old_attachments.py"
    "daily_plan_cutoff_check.py"
)
# Shipped as units: rm -rf $DEST/<dir> && cp -r $SRC/<dir> $DEST/<dir>.
BACKEND_CORE_DIR="core"
# 25.09: first router extraction (routes/auth.py) -- main.py now does
# `app.include_router(...)` from this package, so it must ship exactly like
# core/ (same rm -rf + cp -r unit) or a deploy leaves main.py importing a
# directory that doesn't exist on the serving path.
BACKEND_ROUTES_DIR="routes"
# 13.10 Phase 9: route factories now import framework-agnostic application
# services. Keep this directory in the same transactional runtime manifest so
# deploy/backup/rollback cannot leave routes and services from different SHAs.
BACKEND_SERVICES_DIR="services"
