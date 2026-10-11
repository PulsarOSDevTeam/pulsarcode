# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
#
# Homebrew formula for Pulsar Code. Publish it in a tap, for example
# PulsarOSDevTeam/homebrew-tap as Formula/pulsarcode.rb, then:
#
#   brew install pulsarosdevteam/tap/pulsarcode
#
# url and sha256 are rewritten for every release by tools/release/update_manifests.py
# (the release workflow opens a pull request with the new values). Do not edit them by hand.
#
# Layout mirrors install.sh: libexec/app holds the engine and the `pulsarcode` shim,
# libexec/venv the private Python runtime. The shim never runs `python -m pulsar`, so a
# project folder containing its own `pulsar/` or `json.py` cannot hijack the launch.
class Pulsarcode < Formula
  desc "Run the official Claude Code CLI on any model"
  homepage "https://github.com/PulsarOSDevTeam/pulsarcode"
  url "https://github.com/PulsarOSDevTeam/pulsarcode/releases/download/v3.0.0/pulsarcode-app-3.0.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "AGPL-3.0-or-later"
  head "https://github.com/PulsarOSDevTeam/pulsarcode.git", branch: "main"

  livecheck do
    url :stable
    strategy :github_latest
  end

  depends_on "python@3.12"

  def install
    python3 = Formula["python@3.12"].opt_bin/"python3.12"
    venv = libexec/"venv"
    system python3, "-m", "venv", venv
    system venv/"bin/python", "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check",
           "--quiet", "-r", "requirements.txt"
    (libexec/"app").install "pulsar", "pulsarcode", "requirements.txt"
    chmod 0755, libexec/"app/pulsarcode"
    (bin/"pulsarcode").write_env_script libexec/"app/pulsarcode",
                                        PULSAR_PYTHON:  venv/"bin/python",
                                        PULSAR_MANAGED: "homebrew"
  end

  def caveats
    <<~EOS
      Pulsar Code drives the official Claude Code CLI. If `claude` is not installed yet:
        curl -fsSL https://claude.ai/install.sh | bash
      Then run `pulsarcode` in any project folder and paste an API key.
      Keys and settings live in ~/.pulsarcode. Upgrade with `brew upgrade pulsarcode`.
    EOS
  end

  test do
    ENV["PULSAR_HOME"] = (testpath/".pulsarcode").to_s
    ENV["PULSAR_OFFLINE"] = "1"
    ENV["PULSAR_FX"] = "off"
    ENV["NO_COLOR"] = "1"
    assert_match "pulsarcode #{version}", shell_output("#{bin}/pulsarcode version")
    assert_match "OpenRouter", shell_output("#{bin}/pulsarcode providers")

    # Nothing from the working directory may ever be imported.
    (testpath/"pulsar").mkpath
    (testpath/"pulsar/__init__.py").write "print('PWNED')\nraise SystemExit(66)\n"
    (testpath/"json.py").write "print('PWNED')\nraise SystemExit(66)\n"
    cd testpath do
      output = shell_output("#{bin}/pulsarcode version 2>&1")
      refute_match "PWNED", output
      assert_match version.to_s, output
    end
  end
end
