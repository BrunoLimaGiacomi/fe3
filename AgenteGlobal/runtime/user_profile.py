"""Persistent user-profile selection for the Global runtime.

User profiles are deliberately smaller than agent manifests.  A profile only
contributes the ``what``, ``criteria`` and ``skills`` overlay used to shape a
session.  It does not contain, inherit, or grant permissions, tools, models,
endpoints, or mutation rights.  Agent manifests remain the canonical source
for those concerns.

The selected profile is stored once, after the first successful selection, in
``%APPDATA%\\AgenteGlobal\\user-profile.json``.  The write uses a temporary
file in the destination directory and ``os.replace`` so a partial JSON file is
never installed as the active selection.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType


PROFILE_CATALOG_FILENAME = "profiles.toml"
USER_PROFILE_FILENAME = "user-profile.json"
DEFAULT_PROFILE_DIRECTORY = "AgenteGlobal"
MAX_PROFILE_FILE_BYTES = 4_096
MAX_CATALOG_FILE_BYTES = 128 * 1_024
MAX_PROFILE_TEXT_CHARS = 16_384
MAX_PROFILE_CRITERIA = 64
MAX_PROFILE_SKILLS = 128

_PROFILE_FIELDS = frozenset({"what", "criteria", "skills"})
_PROFILE_NAME_VALUES = frozenset({"global", "grc"})
_SKILL_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-.")


class ProfileError(ValueError):
    """Base class for invalid profile configuration or state."""


class ProfileCatalogError(ProfileError):
    """The profile catalog is missing, malformed, or unsafe."""


class ProfilePersistenceError(ProfileError):
    """The persisted selection is missing, malformed, or cannot be stored."""


class ProfileSelectionRequired(ProfileError):
    """A first selection is required but no interactive or explicit choice exists."""


class InvalidProfileName(ProfileError):
    """A profile name is not one of the supported enum values."""


class ProfileName(str, Enum):
    """The user-level profiles supported by this distribution."""

    GLOBAL = "global"
    GRC = "grc"


def normalize_profile_name(value: ProfileName | str) -> ProfileName:
    """Validate and normalize a profile enum value or its string spelling."""

    if isinstance(value, ProfileName):
        return value
    if not isinstance(value, str):
        raise InvalidProfileName("O perfil precisa ser 'global' ou 'grc'.")
    normalized = value.strip().lower()
    try:
        return ProfileName(normalized)
    except ValueError as error:
        raise InvalidProfileName(
            f"Perfil inválido: {value!r}. Valores aceitos: global, grc."
        ) from error


def _validated_text(value: object, field_name: str, *, maximum: int = MAX_PROFILE_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        raise ProfileCatalogError(f"profiles.*.{field_name} precisa ser texto.")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ProfileCatalogError(
            f"profiles.*.{field_name} precisa ser texto não vazio de até {maximum} caracteres."
        )
    return normalized


def _validated_string_list(
    value: object,
    field_name: str,
    *,
    maximum_items: int,
    identifier: bool = False,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ProfileCatalogError(f"profiles.*.{field_name} precisa ser uma lista.")
    if len(value) > maximum_items:
        raise ProfileCatalogError(f"profiles.*.{field_name} excede {maximum_items} itens.")

    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ProfileCatalogError(f"profiles.*.{field_name} precisa conter apenas textos.")
        item_value = item.strip().lower() if identifier else item.strip()
        if not item_value or len(item_value) > 256:
            raise ProfileCatalogError(f"profiles.*.{field_name} contém um item inválido.")
        if identifier:
            if any(character not in _SKILL_NAME_CHARS for character in item_value):
                raise ProfileCatalogError(
                    f"profiles.*.{field_name} contém um identificador de skill inválido."
                )
            if item_value.startswith((".", "-")) or item_value.endswith((".", "-")):
                raise ProfileCatalogError(
                    f"profiles.*.{field_name} contém um identificador de skill inválido."
                )
        if item_value in normalized:
            raise ProfileCatalogError(f"profiles.*.{field_name} não pode conter duplicatas.")
        normalized.append(item_value)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class UserProfile:
    """Validated profile overlay selected for a user session.

    ``permissions`` and mutation-related fields intentionally do not exist on
    this type.  The runtime must continue to obtain those controls from its
    policy engine and the canonical agent manifests.
    """

    name: ProfileName
    what: str
    criteria: tuple[str, ...]
    skills: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", normalize_profile_name(self.name))
        object.__setattr__(self, "what", _validated_text(self.what, "what"))
        object.__setattr__(
            self,
            "criteria",
            _validated_string_list(
                self.criteria,
                "criteria",
                maximum_items=MAX_PROFILE_CRITERIA,
            ),
        )
        object.__setattr__(
            self,
            "skills",
            _validated_string_list(
                self.skills,
                "skills",
                maximum_items=MAX_PROFILE_SKILLS,
                identifier=True,
            ),
        )

    @property
    def id(self) -> str:
        """Stable serialized identifier for integration points."""

        return self.name.value

    @property
    def profile(self) -> ProfileName:
        """Compatibility view used by callers that call the selection ``profile``."""

        return self.name

    def to_overlay(self) -> dict[str, object]:
        """Return only the fields a profile is allowed to overlay."""

        return {
            "what": self.what,
            "criteria": list(self.criteria),
            "skills": list(self.skills),
        }

    overlay = to_overlay


class ProfileCatalog:
    """Immutable catalog of user profiles, independent from agent manifests."""

    def __init__(self, profiles: Mapping[ProfileName | str, UserProfile]) -> None:
        if not isinstance(profiles, Mapping):
            raise TypeError("profiles precisa ser um mapping")
        normalized: dict[str, UserProfile] = {}
        for raw_name, profile in profiles.items():
            name = normalize_profile_name(raw_name).value
            if not isinstance(profile, UserProfile):
                raise TypeError("O catálogo precisa conter UserProfile")
            if profile.id != name:
                raise ProfileCatalogError("O nome do perfil não corresponde à sua entrada no catálogo.")
            if name in normalized:
                raise ProfileCatalogError(f"Perfil duplicado: {name}")
            normalized[name] = profile
        missing = _PROFILE_NAME_VALUES.difference(normalized)
        if missing:
            raise ProfileCatalogError(
                "O catálogo precisa definir os perfis: " + ", ".join(sorted(_PROFILE_NAME_VALUES))
            )
        self._profiles = MappingProxyType(dict(sorted(normalized.items())))

    @classmethod
    def load(cls, path: Path | str | None = None) -> "ProfileCatalog":
        """Load and validate the packaged profile catalog."""

        catalog_path = Path(path) if path is not None else default_catalog_path()
        if catalog_path.is_symlink() or not catalog_path.is_file():
            raise ProfileCatalogError(f"Catálogo de perfis não encontrado: {catalog_path}")
        try:
            if catalog_path.stat().st_size > MAX_CATALOG_FILE_BYTES:
                raise ProfileCatalogError(
                    f"Catálogo de perfis excede {MAX_CATALOG_FILE_BYTES} bytes."
                )
            with catalog_path.open("rb") as stream:
                raw = tomllib.load(stream)
        except ProfileCatalogError:
            raise
        except (OSError, tomllib.TOMLDecodeError, UnicodeError) as error:
            raise ProfileCatalogError(f"Catálogo de perfis inválido: {catalog_path}") from error
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ProfileCatalog":
        """Build a catalog from parsed TOML data, useful for isolated tests."""

        if not isinstance(raw, Mapping) or set(raw) != {"profiles"}:
            raise ProfileCatalogError("O catálogo precisa conter somente a tabela [profiles].")
        profiles_raw = raw.get("profiles")
        if not isinstance(profiles_raw, Mapping):
            raise ProfileCatalogError("[profiles] precisa ser uma tabela TOML.")

        profiles: dict[str, UserProfile] = {}
        for raw_name, raw_profile in profiles_raw.items():
            try:
                name = normalize_profile_name(raw_name)
            except (InvalidProfileName, TypeError) as error:
                raise ProfileCatalogError("O catálogo contém um nome de perfil inválido.") from error
            if not isinstance(raw_profile, Mapping):
                raise ProfileCatalogError(f"profiles.{name.value} precisa ser uma tabela.")
            if set(raw_profile) != _PROFILE_FIELDS:
                unexpected = sorted(set(raw_profile).difference(_PROFILE_FIELDS))
                missing = sorted(_PROFILE_FIELDS.difference(raw_profile))
                details = []
                if missing:
                    details.append("ausentes=" + ",".join(missing))
                if unexpected:
                    details.append("não permitidos=" + ",".join(unexpected))
                raise ProfileCatalogError(
                    f"profiles.{name.value} possui campos inválidos ({'; '.join(details)})."
                )
            profiles[name.value] = UserProfile(
                name=name,
                what=_validated_text(raw_profile["what"], "what"),
                criteria=_validated_string_list(
                    raw_profile["criteria"],
                    "criteria",
                    maximum_items=MAX_PROFILE_CRITERIA,
                ),
                skills=_validated_string_list(
                    raw_profile["skills"],
                    "skills",
                    maximum_items=MAX_PROFILE_SKILLS,
                    identifier=True,
                ),
            )
        return cls(profiles)

    @property
    def profiles(self) -> Mapping[str, UserProfile]:
        """Read-only profile mapping; agent manifests are not included."""

        return self._profiles

    def list_profiles(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    def get(self, name: ProfileName | str) -> UserProfile | None:
        return self._profiles.get(normalize_profile_name(name).value)

    def require(self, name: ProfileName | str) -> UserProfile:
        normalized = normalize_profile_name(name).value
        profile = self._profiles.get(normalized)
        if profile is None:
            raise ProfileCatalogError(f"Perfil não definido no catálogo: {normalized}")
        return profile

    def overlay_for(self, name: ProfileName | str) -> dict[str, object]:
        return self.require(name).to_overlay()


def default_catalog_path() -> Path:
    """Return the packaged catalog, without consulting the agent-manifest directory."""

    return Path(__file__).resolve().parents[1] / PROFILE_CATALOG_FILENAME


def default_user_profile_path(appdata_dir: Path | str | None = None) -> Path:
    """Return ``%APPDATA%\\AgenteGlobal\\user-profile.json``.

    ``appdata_dir`` is an explicit injection point for tests and embedding
    applications.  In normal operation the Windows ``APPDATA`` variable is
    required; silently falling back to the repository would make a user
    selection unexpectedly shared with source code.
    """

    raw_appdata = appdata_dir if appdata_dir is not None else os.environ.get("APPDATA")
    if isinstance(raw_appdata, Path):
        appdata = raw_appdata
    elif isinstance(raw_appdata, str) and raw_appdata.strip():
        appdata = Path(raw_appdata.strip())
    else:
        raise ProfilePersistenceError("APPDATA não está definido; não é possível localizar o perfil do usuário.")
    return appdata / DEFAULT_PROFILE_DIRECTORY / USER_PROFILE_FILENAME


get_user_profile_path = default_user_profile_path


def _validate_storage_target(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ProfilePersistenceError(f"O destino do perfil não é um arquivo regular: {path}")
    parent = path.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise ProfilePersistenceError(f"O diretório do perfil não é uma pasta regular: {parent}")


def _read_persisted_name(path: Path) -> ProfileName | None:
    if not path.exists() and not path.is_symlink():
        return None
    _validate_storage_target(path)
    try:
        if path.stat().st_size > MAX_PROFILE_FILE_BYTES:
            raise ProfilePersistenceError(
                f"Arquivo de perfil excede {MAX_PROFILE_FILE_BYTES} bytes: {path}"
            )
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_object_no_duplicates)
    except ProfilePersistenceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProfilePersistenceError(f"Arquivo de perfil corrompido: {path}") from error
    if not isinstance(raw, dict) or set(raw) != {"profile"}:
        raise ProfilePersistenceError(
            "Arquivo de perfil precisa conter exatamente a chave JSON 'profile'."
        )
    try:
        return normalize_profile_name(raw["profile"])
    except (InvalidProfileName, TypeError) as error:
        raise ProfilePersistenceError("O enum de perfil persistido é inválido.") from error


def _json_object_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous persisted state instead of silently taking last-wins."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Chave JSON duplicada: {key}")
        result[key] = value
    return result


def _atomic_write_profile(path: Path, profile: ProfileName) -> None:
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ProfilePersistenceError(f"Não foi possível criar o diretório do perfil: {parent}") from error
    _validate_storage_target(path)

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump({"profile": profile.value}, temporary, ensure_ascii=False, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError as error:
        raise ProfilePersistenceError(f"Não foi possível persistir o perfil em {path}") from error
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


class UserProfileStore:
    """Small storage facade the Core can call without knowing JSON details."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        catalog: ProfileCatalog | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_user_profile_path()
        self.catalog = catalog or ProfileCatalog.load()

    def load(self) -> UserProfile | None:
        """Load the persisted profile, returning ``None`` only when absent."""

        name = _read_persisted_name(self.path)
        if name is None:
            return None
        try:
            return self.catalog.require(name)
        except (InvalidProfileName, ProfileCatalogError) as error:
            raise ProfilePersistenceError("O perfil persistido não existe no catálogo atual.") from error

    def save(self, profile: ProfileName | str | UserProfile) -> UserProfile:
        """Persist the initial profile and return the canonical catalog object.

        A previously persisted selection is immutable through this facade.  A
        same-value save is idempotent; replacing it requires an operator-level
        deletion/migration decision outside the normal startup path.
        """

        name = profile.name if isinstance(profile, UserProfile) else normalize_profile_name(profile)
        selected = self.catalog.require(name)
        existing = _read_persisted_name(self.path)
        if existing is not None:
            if existing != selected.name:
                raise ProfilePersistenceError("O perfil do usuário já foi selecionado e não pode ser substituído.")
            return selected
        _atomic_write_profile(self.path, selected.name)
        return selected

    def resolve(
        self,
        *,
        initial_profile: ProfileName | str | None = None,
        interactive: bool | None = None,
        non_interactive: bool | None = None,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
        max_attempts: int = 3,
    ) -> UserProfile:
        """Load once, or select and persist the profile when it is absent.

        Existing state always wins: an explicit value on a later invocation
        cannot silently replace the user's one-time choice.  A non-interactive
        first invocation must provide ``initial_profile``.
        """

        if non_interactive is not None:
            if interactive is not None and bool(interactive) == bool(non_interactive):
                raise ValueError("interactive e non_interactive são opções contraditórias.")
            interactive = not bool(non_interactive)

        existing = self.load()
        if existing is not None:
            return existing

        selected: ProfileName
        if initial_profile is not None:
            selected = normalize_profile_name(initial_profile)
        else:
            if interactive is None:
                try:
                    interactive = bool(sys.stdin.isatty()) if input_fn is input else True
                except (AttributeError, OSError):
                    interactive = False
            if not interactive:
                raise ProfileSelectionRequired(
                    "Primeira seleção de perfil exige initial_profile em modo não interativo."
                )
            selected = self._prompt(input_fn, output_fn, max_attempts=max_attempts)
        return self.save(selected)

    def _prompt(
        self,
        input_fn: Callable[[str], str],
        output_fn: Callable[[str], None],
        *,
        max_attempts: int,
    ) -> ProfileName:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts precisa ser um inteiro positivo.")
        output_fn("+-- Primeiro acesso: escolha seu perfil -----------------------+")
        output_fn("| 1  Global - Cloud, seguranca, desenvolvimento e DevSecOps    |")
        output_fn("| 2  GRC    - Governanca, riscos, controles e compliance       |")
        output_fn("+--------------------------------------------------------------+")
        prompt = "Perfil [1/2]: "
        for _ in range(max_attempts):
            try:
                answer = input_fn(prompt)
            except (EOFError, KeyboardInterrupt) as error:
                raise ProfileSelectionRequired("Seleção de perfil interrompida.") from error
            try:
                alias = {"1": "global", "2": "grc"}.get(answer.strip(), answer)
                return self.catalog.require(normalize_profile_name(alias)).name
            except (InvalidProfileName, ProfileCatalogError):
                output_fn("Perfil inválido. Escolha global ou grc.")
        raise ProfileSelectionRequired("Não foi selecionado um perfil válido.")


def load_or_select_profile(
    *,
    path: Path | str | None = None,
    catalog: ProfileCatalog | None = None,
    initial_profile: ProfileName | str | None = None,
    interactive: bool | None = None,
    non_interactive: bool | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    max_attempts: int = 3,
) -> UserProfile:
    """Functional facade for Core integration."""

    return UserProfileStore(path, catalog=catalog).resolve(
        initial_profile=initial_profile,
        interactive=interactive,
        non_interactive=non_interactive,
        input_fn=input_fn,
        output_fn=output_fn,
        max_attempts=max_attempts,
    )


def load_user_profile(
    *,
    path: Path | str | None = None,
    catalog: ProfileCatalog | None = None,
) -> UserProfile | None:
    """Load only persisted state; never prompts or creates a selection."""

    return UserProfileStore(path, catalog=catalog).load()


def persist_user_profile(
    profile: ProfileName | str | UserProfile,
    *,
    path: Path | str | None = None,
    catalog: ProfileCatalog | None = None,
) -> UserProfile:
    """Validate and persist the one-time selection without prompting."""

    return UserProfileStore(path, catalog=catalog).save(profile)


select_user_profile = load_or_select_profile
resolve_user_profile = load_or_select_profile


__all__ = [
    "DEFAULT_CATALOG_PATH",
    "DEFAULT_PROFILE_DIRECTORY",
    "InvalidProfileName",
    "MAX_CATALOG_FILE_BYTES",
    "MAX_PROFILE_FILE_BYTES",
    "PROFILE_CATALOG_FILENAME",
    "PROFILE_FILE_NAME",
    "ProfileCatalog",
    "ProfileCatalogError",
    "ProfileError",
    "ProfileName",
    "ProfilePersistenceError",
    "ProfileSelectionRequired",
    "UserProfile",
    "UserProfileStore",
    "default_catalog_path",
    "default_user_profile_path",
    "get_user_profile_path",
    "load_or_select_profile",
    "load_user_profile",
    "normalize_profile_name",
    "persist_user_profile",
    "resolve_user_profile",
    "select_user_profile",
]


DEFAULT_CATALOG_PATH = default_catalog_path()
PROFILE_FILE_NAME = USER_PROFILE_FILENAME
