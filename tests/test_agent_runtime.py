from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CORE_PATHS = (
    ROOT / "AgenteGlobal" / "AgenteGlobalCore.py",
    ROOT / "AgenteGRC" / "AgenteGRCCore.py",
)


def load_core(path: Path):
    module_name = f"test_{path.stem.lower()}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Não foi possível carregar {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    module_dir = str(path.parent)
    sys.path.insert(0, module_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(module_dir)
    return module


def make_config(module, workspace: Path):
    return module.AgentConfig(
        workspace=workspace,
        api_key_file=workspace / "api-key.txt",
        model_alias_file=workspace / "model-aliases.json",
        agents_file=workspace / "AGENTS.md",
        skills_dir=workspace / "skills",
        read_scope="workspace",
        write_scope="workspace",
        load_project_context=True,
        history_limit=5,
        allow_shell=False,
        allow_sensitive_read=False,
        permission_mode="strict",
        verbosity_mode="normal",
        api_timeout_seconds=45.0,
        api_retries=1,
        max_steps=64,
        max_subagents=0,
        subagent_max_steps=4,
    )


def response(content: str = "", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def tool_response(index: int):
    tool_call = SimpleNamespace(
        id=f"call-{index}",
        function=SimpleNamespace(name="list_dir", arguments=json.dumps({"path": "."})),
    )
    return response(tool_calls=[tool_call])


def subagent_tool_call(index: int):
    return SimpleNamespace(
        id=f"subagent-call-{index}",
        function=SimpleNamespace(
            name="spawn_subagent",
            arguments=json.dumps(
                {
                    "task": f"Tarefa independente {index}",
                    "name": f"worker-{index}",
                    "allow_mutation": False,
                }
            ),
        ),
    )


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes):
        self.chat = SimpleNamespace(completions=FakeCompletions(outcomes))


class ParallelSubagentCompletions:
    def __init__(self):
        self.barrier = threading.Barrier(2)
        self.lock = threading.Lock()
        self.main_calls = 0
        self.calls = 0

    def create(self, **kwargs):
        with self.lock:
            self.calls += 1
        messages = kwargs.get("messages") or []
        system_content = str(messages[0].get("content", "")) if messages else ""
        if "um subagente especializado chamado" in system_content:
            self.barrier.wait(timeout=2)
            return response("resultado independente")

        with self.lock:
            self.main_calls += 1
            main_call = self.main_calls
        if main_call == 1:
            return response(tool_calls=[subagent_tool_call(1), subagent_tool_call(2)])
        return response("consolidação concluída")


class ParallelSubagentClient:
    def __init__(self):
        self.completions = ParallelSubagentCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


class FakeRunner:
    def __init__(self):
        self.calls = 0

    def execute(self, _name, _arguments):
        self.calls += 1
        return json.dumps({"path": ".", "entries": []})


class AgentRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = [load_core(path) for path in CORE_PATHS]

    def test_retry_preserves_root_cause(self):
        for module in self.modules:
            with self.subTest(module=module.__name__):
                error = module.APIConnectionError(message="Connection error", request=object())
                error.__cause__ = OSError("dns failure")
                client = FakeClient([error, response("ok")])
                with patch.object(module.time, "sleep", return_value=None):
                    result = module.create_chat_completion_with_retry(
                        client,
                        operation="test",
                        api_retries=1,
                        emit_status=False,
                        model="test-model",
                        messages=[],
                    )
                self.assertEqual(result.choices[0].message.content, "ok")
                self.assertEqual(client.chat.completions.calls, 2)
                self.assertIn("OSError: dns failure", module.exception_chain_summary(error))

    def test_adaptive_steps_continue_beyond_eight(self):
        for module in self.modules:
            with self.subTest(module=module.__name__):
                client = FakeClient([*(tool_response(index) for index in range(9)), response("finalizado")])
                runner = FakeRunner()
                messages = [{"role": "system", "content": "test"}]
                content = module.run_agent_until_final(
                    client=client,
                    model="test-model",
                    messages=messages,
                    tools_runner=runner,
                    tool_schemas=[],
                    temperature=0.1,
                    max_steps=16,
                    api_retries=0,
                    emit_tools=False,
                )
                self.assertEqual(content, "finalizado")
                self.assertEqual(runner.calls, 9)
                self.assertEqual(client.chat.completions.calls, 10)

    def test_exit_auto_saves_model_summary_once(self):
        for module in self.modules:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
                workspace = Path(temp_dir)
                config = make_config(module, workspace)
                summary = json.dumps(
                    {"title": "Sessão de teste", "summary_markdown": "## Resumo\n\nContexto preservado."}
                )
                client = FakeClient([response("Concluído e validado."), response(summary)])
                inputs = iter(["execute o teste", "/exit"])
                with (
                    patch.object(module, "read_user_input", side_effect=lambda *_args, **_kwargs: next(inputs)),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    module.agent_loop(
                        client=client,
                        model="test-model",
                        model_resolution="test",
                        config=config,
                        temperature=0.1,
                    )

                history_files = list((workspace / module.HISTORY_DIR_NAME).glob("*.md"))
                self.assertEqual(len(history_files), 1)
                saved = history_files[0].read_text(encoding="utf-8")
                self.assertIn("Contexto preservado", saved)
                self.assertIn("Origem do salvamento: `exit`", saved)
                next_session = module.create_initial_messages(config)
                self.assertIn("<saved_history_context>", next_session[0]["content"])
                self.assertIn("Contexto preservado", next_session[0]["content"])
                self.assertEqual(client.chat.completions.calls, 2)

    def test_history_limit_loads_six_most_recent_files(self):
        for module in self.modules:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
                workspace = Path(temp_dir)
                config = module.replace(make_config(module, workspace), history_limit=6)
                history_dir = workspace / module.HISTORY_DIR_NAME
                history_dir.mkdir()
                for index in range(1, 8):
                    history_file = history_dir / f"history-{index}.md"
                    history_file.write_text(f"conteudo-unico-{index}", encoding="utf-8")
                    os.utime(history_file, (index, index))

                content = module.read_saved_history_context(config)
                self.assertNotIn("conteudo-unico-1", content)
                for index in range(2, 8):
                    self.assertIn(f"conteudo-unico-{index}", content)
                self.assertLess(content.index("conteudo-unico-7"), content.index("conteudo-unico-2"))

    def test_history_limit_cli_reaches_config(self):
        for module in self.modules:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
                with patch.object(
                    sys,
                    "argv",
                    ["agent", "--workspace", temp_dir, "--history-limit", "6"],
                ):
                    config = module.build_config(module.parse_args())
                self.assertEqual(config.history_limit, 6)

    def test_unchanged_history_is_not_saved_twice(self):
        for module in self.modules:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
                workspace = Path(temp_dir)
                config = make_config(module, workspace)
                client = FakeClient(
                    [
                        response("Concluído."),
                        response(json.dumps({"title": "Teste", "summary_markdown": "## Resumo\n\nOK."})),
                    ]
                )
                inputs = iter(["faça algo", "/save", "/exit"])
                with (
                    patch.object(module, "read_user_input", side_effect=lambda *_args, **_kwargs: next(inputs)),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    module.agent_loop(
                        client=client,
                        model="test-model",
                        model_resolution="test",
                        config=config,
                        temperature=0.1,
                    )
                self.assertEqual(len(list((workspace / module.HISTORY_DIR_NAME).glob("*.md"))), 1)
                self.assertEqual(client.chat.completions.calls, 2)

    def test_api_failure_keeps_completed_tool_results(self):
        for module in self.modules:
            with self.subTest(module=module.__name__):
                error = module.APIConnectionError(message="Connection error", request=object())
                error.__cause__ = OSError("network unavailable")
                client = FakeClient([tool_response(1), error])
                runner = FakeRunner()
                messages = [
                    {"role": "system", "content": "test"},
                    {"role": "user", "content": "execute"},
                ]
                with self.assertRaises(module.APIConnectionError):
                    module.run_agent_until_final(
                        client=client,
                        model="test-model",
                        messages=messages,
                        tools_runner=runner,
                        tool_schemas=[],
                        temperature=0.1,
                        max_steps=16,
                        api_retries=0,
                        emit_tools=False,
                    )
                module.append_api_failure_context(messages, 1, error)
                self.assertEqual(runner.calls, 1)
                self.assertTrue(any(message.get("role") == "tool" for message in messages))
                self.assertIn("não repita ações", messages[-1]["content"])

    def test_diagnostic_log_is_created_without_conversation_content(self):
        for module in self.modules:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
                workspace = Path(temp_dir)
                try:
                    log_path = module.configure_diagnostic_logging(workspace)
                    self.assertIsNotNone(log_path)
                    module.LOGGER.warning("diagnostic_test event=connection_failure")
                    for handler in module.LOGGER.handlers:
                        handler.flush()
                    content = log_path.read_text(encoding="utf-8")
                    self.assertIn("runtime_started", content)
                    self.assertIn("diagnostic_test", content)
                    self.assertNotIn("prompt", content)
                finally:
                    for handler in list(module.LOGGER.handlers):
                        module.LOGGER.removeHandler(handler)
                        handler.close()
                    module.LOGGER.addHandler(module.logging.NullHandler())
                    module.DIAGNOSTIC_LOG_PATH = None

    def test_success_text_is_green_and_secrets_are_redacted(self):
        for module in self.modules:
            with self.subTest(module=module.__name__):
                self.assertEqual(
                    module.infer_assistant_content_style("Não houve erro. Trabalho concluído e validado."),
                    "green",
                )
                redacted = module.redact_sensitive_text('{"api_key":"very-secret-value"}')
                self.assertNotIn("very-secret-value", redacted)

    def test_agenteglobal_manual_spawn_command_runs_subagent(self):
        module = next(item for item in self.modules if item.AGENT_NAME == "AgenteGlobal")
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            workspace = Path(temp_dir)
            config = module.replace(
                make_config(module, workspace),
                max_subagents=1,
                permission_mode="auto",
            )
            summary = json.dumps({"title": "Spawn", "summary_markdown": "## Resumo\n\nSubagente validado."})
            client = FakeClient([response("auditoria concluída"), response(summary)])
            inputs = iter(["/spawn audite o módulo sem alterar arquivos", "/exit"])
            stdout = io.StringIO()
            with (
                patch.object(module, "read_user_input", side_effect=lambda *_args, **_kwargs: next(inputs)),
                redirect_stdout(stdout),
                redirect_stderr(io.StringIO()),
            ):
                module.agent_loop(
                    client=client,
                    model="test-model",
                    model_resolution="test",
                    config=config,
                    temperature=0.1,
                )

            self.assertIn("auditoria concluída", stdout.getvalue())
            self.assertIn("/spawn", module.SLASH_COMMANDS)
            self.assertEqual(client.chat.completions.calls, 2)

    def test_agenteglobal_spawn_defaults_to_mutation(self):
        module = next(item for item in self.modules if item.AGENT_NAME == "AgenteGlobal")
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            workspace = Path(temp_dir)
            config = module.replace(
                make_config(module, workspace),
                max_subagents=1,
                permission_mode="auto",
            )
            client = FakeClient([response("alteração concluída")])
            runner = module.WorkspaceTools(config, client=client, model="test-model")

            result = json.loads(runner.spawn_subagent(task="execute a tarefa"))
            schemas = module.build_tool_schemas(
                allow_shell=False,
                allow_write=True,
                allow_subagents=True,
                subagent_max_steps=config.subagent_max_steps,
            )
            spawn_schema = next(item for item in schemas if item["function"]["name"] == "spawn_subagent")

            self.assertTrue(result["allow_mutation"])
            self.assertTrue(spawn_schema["function"]["parameters"]["properties"]["allow_mutation"]["default"])

    def test_agenteglobal_manual_spawn_supports_read_only(self):
        module = next(item for item in self.modules if item.AGENT_NAME == "AgenteGlobal")
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            workspace = Path(temp_dir)
            config = module.replace(make_config(module, workspace), max_subagents=1)
            summary = json.dumps({"title": "Read only", "summary_markdown": "## Resumo\n\nConcluído."})
            client = FakeClient([response(summary)])
            spawn_result = json.dumps(
                {
                    "subagent": "manual",
                    "status": "completed",
                    "allow_mutation": False,
                    "answer": "leitura concluída",
                }
            )
            inputs = iter(["/spawn --read-only inspecione o projeto", "/exit"])
            with (
                patch.object(module, "read_user_input", side_effect=lambda *_args, **_kwargs: next(inputs)),
                patch.object(module.WorkspaceTools, "spawn_subagent", return_value=spawn_result) as spawned,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                module.agent_loop(
                    client=client,
                    model="test-model",
                    model_resolution="test",
                    config=config,
                    temperature=0.1,
                )

            self.assertFalse(spawned.call_args.kwargs["allow_mutation"])
            self.assertEqual(spawned.call_args.kwargs["task"], "inspecione o projeto")

    def test_agenteglobal_plan_blocks_mutating_spawn(self):
        module = next(item for item in self.modules if item.AGENT_NAME == "AgenteGlobal")
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            config = module.replace(make_config(module, Path(temp_dir)), max_subagents=1)
            inputs = iter(["/plan", "/spawn altere o projeto", "/exit"])
            stdout = io.StringIO()
            with (
                patch.object(module, "read_user_input", side_effect=lambda *_args, **_kwargs: next(inputs)),
                patch.object(module.WorkspaceTools, "spawn_subagent") as spawned,
                redirect_stdout(stdout),
                redirect_stderr(io.StringIO()),
            ):
                module.agent_loop(
                    client=FakeClient([]),
                    model="test-model",
                    model_resolution="test",
                    config=config,
                    temperature=0.1,
                )

            spawned.assert_not_called()
            self.assertIn("não permite subagente com mutação", stdout.getvalue())

    def test_agenteglobal_uses_panel_defaults(self):
        module = next(item for item in self.modules if item.AGENT_NAME == "AgenteGlobal")
        panel = sys.modules["Painel"]
        self.assertEqual(module.DEFAULT_MAX_STEPS, panel.DEFAULT_MAX_STEPS)
        self.assertEqual(module.DEFAULT_MAX_SUBAGENTS, panel.DEFAULT_MAX_SUBAGENTS)
        self.assertEqual(module.DEFAULT_HISTORY_FILES, panel.DEFAULT_HISTORY_FILES)

    def test_agenteglobal_runs_subagent_batch_in_parallel(self):
        module = next(item for item in self.modules if item.AGENT_NAME == "AgenteGlobal")
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            workspace = Path(temp_dir)
            config = module.replace(make_config(module, workspace), max_subagents=2)
            client = ParallelSubagentClient()
            runner = module.WorkspaceTools(
                config,
                client=client,
                model="test-model",
                temperature=0.1,
            )
            messages = [{"role": "system", "content": "orquestrador"}]

            result = module.run_agent_until_final(
                client=client,
                model="test-model",
                messages=messages,
                tools_runner=runner,
                tool_schemas=module.build_tool_schemas(
                    allow_shell=False,
                    allow_write=False,
                    allow_subagents=True,
                    subagent_max_steps=config.subagent_max_steps,
                ),
                temperature=0.1,
                max_steps=8,
                api_retries=0,
                emit_tools=False,
            )

            self.assertEqual(result, "consolidação concluída")
            self.assertEqual(client.completions.calls, 4)
            tool_results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
            self.assertEqual([item["status"] for item in tool_results], ["completed", "completed"])


if __name__ == "__main__":
    unittest.main()
