#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
#
# Configure the GitHub repository: description, topics, wiki off, the "pypi" and "npm"
# deployment environments (v* tags only), a tag ruleset so only admins can create, move or
# delete v* tags, and read-only default workflow permissions. Idempotent: run it as often as you like.
#
#   tools/github/setup.sh --dry-run               print every command, change nothing
#   tools/github/setup.sh                         apply (needs `gh auth login` with admin rights)
#   tools/github/setup.sh --repo owner/name
#   tools/github/setup.sh --reviewer LOGIN        also require LOGIN to approve each publish
#   tools/github/setup.sh --community             also turn on Issues and Discussions, the channels
#                                                 CONTRIBUTING.md sends bug reports and questions to
#   tools/github/setup.sh --pages                 also publish docs/ with GitHub Pages (main /docs) and
#                                                 make it the homepage. Refused while the repository is
#                                                 not public: a Pages site is public on the internet.
#
# Prints the steps the API cannot do (social preview image, PyPI and npm trusted publishers).

set -euo pipefail

REPO="PulsarOSDevTeam/pulsarcode"
DRY=0
REVIEWER=""
ENVIRONMENTS=(pypi npm)
RULESET_NAME="release tags"
DESCRIPTION="Run the official Claude Code CLI on any model. Paste a key from NVIDIA, OpenRouter, OpenAI, Gemini, DeepSeek, Kimi, GLM, Groq or Ollama and go. Local, token-gated, no telemetry."
PAGES_URL="https://pulsarosdevteam.github.io/pulsarcode/"     # the homepage, with --pages only
TOPICS=(
    claude-code cli terminal tui llm ai-coding coding-agent developer-tools openrouter nvidia-nim
    ollama deepseek kimi glm gemini openai-compatible llm-proxy local-llm byok python
)
COMMUNITY=0
PAGES=0

usage() { sed -n '5,17p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run|-n) DRY=1 ;;
        --repo) shift; REPO="${1:?--repo needs owner/name}" ;;
        --reviewer) shift; REVIEWER="${1:?--reviewer needs a GitHub login}" ;;
        --community) COMMUNITY=1 ;;
        --pages) PAGES=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "bad --repo: $REPO" >&2; exit 2; }
[[ -z "$REVIEWER" || "$REVIEWER" =~ ^[A-Za-z0-9-]{1,39}$ ]] || { echo "bad --reviewer: $REVIEWER" >&2; exit 2; }
[[ ${#TOPICS[@]} -le 20 ]] || { echo "GitHub allows at most 20 topics" >&2; exit 2; }

say() { printf '\n== %s\n' "$*"; }

# run cmd...           execute, or print it in dry-run mode
run() {
    if [[ $DRY -eq 1 ]]; then
        printf '+'; printf ' %q' "$@"; printf '\n'
    else
        "$@"
    fi
}

# run_json body cmd... execute with a JSON body on stdin, or print both in dry-run mode
run_json() {
    local body="$1"; shift
    if [[ $DRY -eq 1 ]]; then
        printf '+'; printf ' %q' "$@"; printf ' <<JSON\n%s\nJSON\n' "$body"
    else
        printf '%s' "$body" | "$@"
    fi
}

json_str() { # minimal JSON string encoder for the fixed values above
    local s="${1//\\/\\\\}"; s="${s//\"/\\\"}"; printf '"%s"' "$s"
}

if [[ $DRY -eq 0 ]]; then
    command -v gh >/dev/null 2>&1 || { echo "gh (GitHub CLI) is required: https://cli.github.com" >&2; exit 1; }
    gh auth status >/dev/null 2>&1 || { echo "run 'gh auth login' first" >&2; exit 1; }
fi

# A GitHub Pages site is public on the internet whatever the repository's visibility (on the plans
# that offer Pages for private repositories, without Enterprise Cloud's access control), so --pages
# is refused before anything changes unless the repository itself is public.
if [[ $PAGES -eq 1 ]]; then
    say "visibility (--pages needs a public repository)"
    if [[ $DRY -eq 1 ]]; then
        run gh repo view "$REPO" --json visibility -q .visibility
    else
        visibility="$(gh repo view "$REPO" --json visibility -q .visibility)" ||
            { echo "could not read the visibility of $REPO; nothing was changed" >&2; exit 1; }
        if [[ "$visibility" != PUBLIC ]]; then
            echo "refusing --pages: $REPO is ${visibility:-not public}, and a GitHub Pages site would be public." \
                 "Nothing was changed. Run without --pages, or with it once the repository is public." >&2
            exit 2
        fi
    fi
fi

say "description, wiki off ($REPO)"
edit=(gh repo edit "$REPO" --description "$DESCRIPTION" --enable-wiki=false)
if [[ $PAGES -eq 1 ]]; then edit+=(--homepage "$PAGES_URL"); fi
run "${edit[@]}"
if [[ $COMMUNITY -eq 1 ]]; then
    say "Issues and Discussions on (--community)"
    run gh repo edit "$REPO" --enable-issues --enable-discussions
fi

say "topics (${#TOPICS[@]}, replaces the current set)"
names=""
for t in "${TOPICS[@]}"; do names+="${names:+,}$(json_str "$t")"; done
run_json "{\"names\":[${names}]}" \
    gh api --method PUT "repos/$REPO/topics" -H "Accept: application/vnd.github+json" --input -

env_body='{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
for envname in "${ENVIRONMENTS[@]}"; do
    say "deployment environment '$envname' (release tags v* only)"
    if [[ $DRY -eq 1 ]]; then
        echo "# created only when missing, so reviewers or wait timers added by hand are never reset"
        run gh api "repos/$REPO/environments/$envname"
        run_json "$env_body" gh api --method PUT "repos/$REPO/environments/$envname" --input -
    elif gh api "repos/$REPO/environments/$envname" >/dev/null 2>&1; then
        echo "environment $envname already exists"
    else
        run_json "$env_body" gh api --method PUT "repos/$REPO/environments/$envname" --input - >/dev/null
        echo "environment $envname created"
    fi
    if [[ $DRY -eq 1 ]]; then
        run gh api "repos/$REPO/environments/$envname/deployment-branch-policies" --jq '.branch_policies[].name'
        run_json '{"name":"v*","type":"tag"}' \
            gh api --method POST "repos/$REPO/environments/$envname/deployment-branch-policies" --input -
    elif gh api "repos/$REPO/environments/$envname/deployment-branch-policies" \
            --jq '.branch_policies[] | select(.type == "tag") | .name' 2>/dev/null | grep -qx 'v\*'; then
        echo "tag policy v* already present"
    else
        run_json '{"name":"v*","type":"tag"}' \
            gh api --method POST "repos/$REPO/environments/$envname/deployment-branch-policies" --input - >/dev/null
        echo "tag policy v* added"
    fi
done

if [[ -n "$REVIEWER" ]]; then
    say "required reviewer $REVIEWER on: ${ENVIRONMENTS[*]}"
    if [[ $DRY -eq 1 ]]; then
        run gh api "users/$REVIEWER" --jq .id
        reviewer_id='"<id of '"$REVIEWER"'>"'
    else
        reviewer_id="$(gh api "users/$REVIEWER" --jq .id)"
        [[ "$reviewer_id" =~ ^[0-9]+$ ]] || { echo "could not resolve GitHub user $REVIEWER" >&2; exit 1; }
    fi
    for envname in "${ENVIRONMENTS[@]}"; do
        # Explicitly requested, so this PUT sets the reviewer list (the branch policy is kept).
        run_json '{"reviewers":[{"type":"User","id":'"$reviewer_id"'}],"prevent_self_review":false,"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}' \
            gh api --method PUT "repos/$REPO/environments/$envname" --input -
    done
fi

say "tag ruleset '$RULESET_NAME': only repository admins may create, move or delete v* tags"
ruleset_body='{"name":"'"$RULESET_NAME"'","target":"tag","enforcement":"active","conditions":{"ref_name":{"include":["refs/tags/v*"],"exclude":[]}},"rules":[{"type":"creation"},{"type":"update"},{"type":"deletion"}],"bypass_actors":[{"actor_id":5,"actor_type":"RepositoryRole","bypass_mode":"always"}]}'
ruleset_jq='.[] | select(.name == "'"$RULESET_NAME"'") | .id'
if [[ $DRY -eq 1 ]]; then
    echo "# updated in place when a ruleset with this name exists, created otherwise (RepositoryRole 5 = admin)"
    run gh api "repos/$REPO/rulesets" --jq "$ruleset_jq"
    run_json "$ruleset_body" gh api --method PUT "repos/$REPO/rulesets/<id>" --input -
    run_json "$ruleset_body" gh api --method POST "repos/$REPO/rulesets" --input -
else
    ruleset_id="$(gh api "repos/$REPO/rulesets" --jq "$ruleset_jq" 2>/dev/null | head -n1 || true)"
    if [[ "$ruleset_id" =~ ^[0-9]+$ ]]; then
        run_json "$ruleset_body" gh api --method PUT "repos/$REPO/rulesets/$ruleset_id" --input - >/dev/null
        echo "ruleset updated ($ruleset_id)"
    else
        run_json "$ruleset_body" gh api --method POST "repos/$REPO/rulesets" --input - >/dev/null
        echo "ruleset created"
    fi
fi

say "default GITHUB_TOKEN permissions: read-only (workflows ask for more per job)"
run_json '{"default_workflow_permissions":"read"}' \
    gh api --method PUT "repos/$REPO/actions/permissions/workflow" --input - \
    || echo "warning: could not set workflow permissions (an organization policy may own this setting)" >&2

# Last, after every hardening step: a plan without Pages for this repository only costs a warning.
if [[ $PAGES -eq 1 ]]; then
    say "GitHub Pages from main /docs (--pages)"
    pages_body='{"build_type":"legacy","source":{"branch":"main","path":"/docs"}}'
    if [[ $DRY -eq 1 ]]; then
        echo "# if Pages is already enabled (GET repos/$REPO/pages succeeds) the PUT updates it, else the POST creates it"
        run gh api "repos/$REPO/pages"
        run_json "$pages_body" gh api --method PUT "repos/$REPO/pages" --input -
        run_json "$pages_body" gh api --method POST "repos/$REPO/pages" --input -
    elif gh api "repos/$REPO/pages" >/dev/null 2>&1; then
        if run_json "$pages_body" gh api --method PUT "repos/$REPO/pages" --input - >/dev/null; then
            echo "Pages source updated"
        else
            echo "warning: could not update GitHub Pages; everything else is configured" >&2
        fi
    elif run_json "$pages_body" gh api --method POST "repos/$REPO/pages" --input - >/dev/null; then
        echo "Pages enabled"
    else
        echo "warning: could not enable GitHub Pages (does the plan offer it?); everything else is configured" >&2
    fi
fi

cat <<EOF

== manual steps (not available through the API)

  1. Social preview: Settings > General > Social preview > Edit > Upload an image,
     choose assets/brand/social.png (1280x640).
  2. PyPI trusted publisher: https://pypi.org/manage/account/publishing/ (or the project's
     Publishing page) with owner PulsarOSDevTeam, repository pulsarcode, workflow release.yml,
     environment pypi.
  3. npm (no token is stored anywhere; the release uses npm trusted publishing):
     a. Publish the first version by hand from a clean checkout of the tag, with 2FA:
          cd packaging/npm && cp ../../LICENSE . && npm publish --access public
        (npm only offers trusted publishing for a package that already exists.)
     b. On https://www.npmjs.com/package/pulsarcode/access add a trusted publisher:
        GitHub Actions, organization PulsarOSDevTeam, repository pulsarcode, workflow
        release.yml, environment npm, and allow "npm publish".
     c. Same page: Publishing access > "Require two-factor authentication and disallow
        tokens".
     d. Turn the release job on:  gh variable set NPM_PUBLISH --body true --repo $REPO
  4. Optional: Settings > Actions > General > "Allow GitHub Actions to create and approve pull
     requests", so the release opens the Homebrew/Scoop manifest pull request itself.
  5. Create PulsarOSDevTeam/homebrew-tap (Formula/pulsarcode.rb) and a Scoop bucket repository,
     then copy packaging/homebrew/pulsarcode.rb and packaging/scoop/pulsarcode.json into them
     after each release.
  6. Settings > General, section Releases: "Enable release immutability", so the files and the
     tag of a published release can never change (the release workflow never replaces them).
  7. Check the ruleset: https://github.com/$REPO/settings/rules (only admins may push v* tags).
EOF
if [[ $PAGES -eq 1 ]]; then echo "  8. Check that Pages builds: https://github.com/$REPO/settings/pages"; fi
if [[ $DRY -eq 1 ]]; then echo; echo "(dry run: nothing was changed)"; fi
