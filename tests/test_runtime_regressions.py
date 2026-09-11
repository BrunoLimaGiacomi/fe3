from __future__ import annotations

import builtins
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_core(name: str, source: Path) -> ModuleType:
    sys.modules.pop("Painel", None)
    sys.path.insert(0, str(source.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, source)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Não foi possível carregar {source}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(source.parent))


GLOBAL = load_core("agenteglobal_core_test", REPO_ROOT / "AgenteGlobal" / "AgenteGlobalCore.py")
GRC = load_core("agentegrc_core_test", REPO_ROOT / "AgenteGRC" / "AgenteGRCCore.py")


def make_config(module: ModuleType, workspace: Path, profiles_dir: Path | None = None):
    workspace.mkdir(parents=True, exist_ok=True)
    anchor = workspace / ".path-resolution-anchor"
    anchor.touch()
    workspace = anchor.resolve().parent
    anchor.unlink()
    resolved_profiles_dir = (workspace / (profiles_dir.name if profiles_dir else "agents")).resolve()
    return module.AgentConfig(
        workspace=workspace,
        api_key_file=workspace / "credencial-fora-de-uso.txt",
        model_alias_file=workspace / "model-aliases.json",
        agents_file=workspace / "AGENTS.md",
        skills_dir=workspace / "skills",
        profiles_dir=resolved_profiles_dir,
        read_scope="workspace",
        write_scope="workspace",
        load_project_context=False,
        history_limit=1,
        allow_shell=True,
        allow_sensitive_read=False,
        permission_mode="strict",
        verbosity_mode="normal",
        api_timeout_seconds=180.0,
        api_retries=1,
        max_steps=4,
        max_subagents=2,
        subagent_max_steps=2,
    )


class RuntimeRegressionTests(unittest.TestCase):
    def test_entrypoints_are_flattened(self) -> None:
        self.assertTrue((REPO_ROOT / "AgenteGlobal" / "AgenteGlobal.py").is_file())
        self.assertFalse((REPO_ROOT / "AgenteGlobal" / "AgenteGlobal" / "AgenteGlobal.py").exists())
        self.assertTrue((REPO_ROOT / "AgenteGRC" / "AgenteGRC.py").is_file())
        self.assertFalse((REPO_ROOT / "AgenteGRC" / "AgenteGRC" / "AgenteGRC.py").exists())

    def test_defaults_distinguish_local_and_api_timeouts(self) -> None:
        for module in (GLOBAL, GRC):
            self.assertEqual(module.DEFAULT_TIMEOUT_SECONDS, 60)
            self.assertEqual(module.DEFAULT_API_TIMEOUT_SECONDS, 180.0)
            client = module.build_client("chave-de-teste", "https://example.invalid/v1", 180.0)
            self.assertEqual(client.timeout, 180.0)

    def test_packaged_profiles_load_for_both_agents(self) -> None:
        for module, folder in ((GLOBAL, "AgenteGlobal"), (GRC, "AgenteGRC")):
            workspace = REPO_ROOT / folder
            profiles = module.load_agent_profiles(make_config(module, workspace))
            self.assertEqual(
                set(profiles),
                {"anaconda", "baitz", "bond", "bulk-worker", "capitao-kowalski", "longato"},
            )
            self.assertEqual(module.select_agent_profile(profiles, "baitz", "qualquer tarefa").name, "Baitz")
            self.assertEqual(module.select_agent_profile(profiles, "", "Crie uma automação Python").name, "Anaconda")

    def test_profile_instructions_do_not_replace_runtime_limits(self) -> None:
        for module, folder in ((GLOBAL, "AgenteGlobal"), (GRC, "AgenteGRC")):
            workspace = REPO_ROOT / folder
            config = make_config(module, workspace)
            profile = module.load_agent_profiles(config)["bond"]
            messages = module.create_subagent_messages(
                config=config,
                name=profile.name,
                task="Revise acessos",
                scope="somente leitura",
                allow_mutation=False,
                profile=profile,
            )
            prompt = messages[0]["content"]
            self.assertIn("Personalidade ativa: Bond", prompt)
            self.assertIn("Modo de permissão atual: strict", prompt)
            self.assertIn("Não chame outros subagentes", prompt)
            self.assertIn("estão desativados para este subagente", prompt)

    def test_spawn_uses_profile_and_same_parent_model(self) -> None:
        for module, folder in ((GLOBAL, "AgenteGlobal"), (GRC, "AgenteGRC")):
            workspace = REPO_ROOT / folder
            config = make_config(module, workspace)
            profiles = module.load_agent_profiles(config)
            captured: dict[str, object] = {}

            def fake_run_agent_until_final(**kwargs):
                captured.update(kwargs)
                return "SUBAGENTE_OK"

            parent_client = object()
            runner = module.WorkspaceTools(
                config,
                client=parent_client,
                model="glm-da-sessao",
                agent_profiles=profiles,
            )
            with patch.object(module, "run_agent_until_final", side_effect=fake_run_agent_until_final):
                result = json.loads(
                    runner.spawn_subagent(
                        task="Prepare um README",
                        allow_mutation=False,
                        profile="baitz",
                    )
                )
            self.assertEqual(captured["model"], "glm-da-sessao")
            self.assertIs(captured["client"], parent_client)
            self.assertEqual(result["model"], "glm-da-sessao")
            self.assertEqual(result["profile"], "baitz")
            self.assertIn("Personalidade ativa: Baitz", captured["messages"][0]["content"])

    def test_profile_cannot_configure_model_or_permissions(self) -> None:
        for module in (GLOBAL, GRC):
            with tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                profiles_dir = workspace / "agents"
                profiles_dir.mkdir()
                (profiles_dir / "invalido.toml").write_text(
                    'name = "Inválido"\n'
                    'description = "Teste"\n'
                    'developer_instructions = "Teste"\n'
                    'model = "outro-modelo"\n',
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "Campos não permitidos"):
                    module.load_agent_profiles(make_config(module, workspace, profiles_dir))

    def test_profile_count_is_bounded(self) -> None:
        for module in (GLOBAL, GRC):
            with tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                profiles_dir = workspace / "agents"
                profiles_dir.mkdir()
                for index in range(module.MAX_PROFILE_COUNT + 1):
                    (profiles_dir / f"perfil-{index}.toml").write_text(
                        f'name = "Perfil {index}"\n'
                        'description = "Teste"\n'
                        'developer_instructions = "Teste"\n',
                        encoding="utf-8",
                    )
                with self.assertRaisesRegex(ValueError, "Máximo"):
                    module.load_agent_profiles(make_config(module, workspace, profiles_dir))

    def test_list_dir_reports_truncation_only_when_needed(self) -> None:
        for module in (GLOBAL, GRC):
            with tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                config = make_config(module, workspace)
                runner = module.WorkspaceTools(config)
                for index in range(100):
                    (workspace / f"item-{index:03d}.txt").touch()
                exact = json.loads(runner.list_dir(max_entries=100))
                self.assertFalse(exact["truncated"])
                self.assertEqual(exact["total_entries"], 100)
                (workspace / "item-100.txt").touch()
                over = json.loads(runner.list_dir(max_entries=100))
                self.assertTrue(over["truncated"])
                self.assertEqual(len(over["entries"]), 100)
                self.assertEqual(over["total_entries"], 101)

    def test_context_compaction_preserves_rules_and_active_turn(self) -> None:
        for module in (GLOBAL, GRC):
            messages = [
                {"role": "system", "content": "regras"},
                {"role": "user", "content": "pedido antigo " * 50},
                {"role": "assistant", "content": "resposta antiga " * 50},
                {"role": "user", "content": "pedido atual"},
            ]
            mandatory_size = sum(module.message_size_chars(message) for message in (messages[0], messages[-1]))
            with patch.object(module, "MAX_API_MESSAGE_CHARS", mandatory_size + 300):
                compacted, omitted = module.prepare_messages_for_api(messages)
            self.assertEqual(omitted, 2)
            self.assertEqual(compacted[0], messages[0])
            self.assertEqual(compacted[-1], messages[-1])
            self.assertIn("mensagens antigas foram omitidas", compacted[1]["content"])

    def test_context_compaction_rejects_oversized_active_turn(self) -> None:
        for module in (GLOBAL, GRC):
            messages = [
                {"role": "system", "content": "regras"},
                {"role": "user", "content": "x" * 500},
            ]
            with patch.object(module, "MAX_API_MESSAGE_CHARS", 400):
                with self.assertRaises(module.PromptTooLargeError):
                    module.prepare_messages_for_api(messages)

    def test_context_compaction_preserves_latest_runtime_state(self) -> None:
        for module in (GLOBAL, GRC):
            messages = [
                {"role": "system", "content": "regras"},
                {"role": "system", "content": "Modo de permissão alterado para strict: antigo."},
                {"role": "user", "content": "pedido antigo " * 50},
                {"role": "assistant", "content": "resposta antiga " * 50},
                {"role": "system", "content": "Modo de permissão alterado para balanced: atual."},
                {"role": "system", "content": "Modo de verbosidade alterado para direto: atual."},
                {"role": "user", "content": "pedido atual"},
            ]
            anchors = (messages[0], messages[4], messages[5], messages[6])
            mandatory_size = sum(module.message_size_chars(message) for message in anchors)
            with patch.object(module, "MAX_API_MESSAGE_CHARS", mandatory_size + 300):
                compacted, omitted = module.prepare_messages_for_api(messages)
            contents = [str(message.get("content") or "") for message in compacted]
            self.assertEqual(omitted, 3)
            self.assertNotIn(messages[1]["content"], contents)
            self.assertIn(messages[4]["content"], contents)
            self.assertIn(messages[5]["content"], contents)
            self.assertEqual(compacted[-1], messages[-1])

    def test_oversized_turn_preserves_completed_tool_evidence(self) -> None:
        for module in (GLOBAL, GRC):
            messages = [
                {"role": "system", "content": "regras"},
                {"role": "user", "content": "faça"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
                {"role": "tool", "tool_call_id": "call-1", "content": "evidência"},
            ]
            module.close_oversized_turn(messages, 1, module.PromptTooLargeError("limite"))
            self.assertEqual(messages[1]["content"], "faça")
            self.assertEqual(messages[3]["content"], "evidência")
            self.assertEqual(messages[-1]["role"], "assistant")
            self.assertIn("evidências foram preservadas", messages[-1]["content"])

    def test_approval_pauses_board_once(self) -> None:
        class Board:
            pauses = 0
            resumes = 0

            def pause_for_approval(self) -> None:
                self.pauses += 1

            def resume_after_approval(self) -> None:
                self.resumes += 1

        for module in (GLOBAL, GRC):
            board = Board()
            module._TASK_BOARD_CONTEXT.task_board = board
            try:
                with patch.object(builtins, "input", return_value="y") as mocked_input:
                    self.assertTrue(module.confirm_action("teste", "detalhe"))
                mocked_input.assert_called_once()
                self.assertEqual((board.pauses, board.resumes), (1, 1))
            finally:
                delattr(module._TASK_BOARD_CONTEXT, "task_board")

    def test_eof_during_strict_approval_is_controlled(self) -> None:
        for module in (GLOBAL, GRC):
            with patch.object(builtins, "input", side_effect=EOFError):
                with self.assertRaises(module.ApprovalUnavailableError):
                    module.confirm_action("run_cli", "detalhe")


if __name__ == "__main__":
    unittest.main()
