"""mo2linttricks: Mod Organizer 2 plugins for Linux (MO2-LINT) setups.

- Modlist Sync: mirrors the modlist to a private GitHub repo, automatically.
- Restore: rebuilds a synced profile (downloads, installs, order).
- GameMode: puts the game, not MO2, into Feral GameMode under Wine/Proton.
- Conflict resolver: Cyberpunk 2077 .archive, TweakXL and redscript conflicts, with a TweakXL patch.
"""

from .conflicts_plugin import ConflictResolverTool, ConflictsDiagnose
from .gamemode_plugin import GameModeLauncher
from .sync_plugin import ModlistSync
from .tools import RestoreTool, SignInTool, SyncNowTool


def createPlugins():
    sync = ModlistSync()
    return [
        sync,
        SignInTool(sync),
        SyncNowTool(sync),
        RestoreTool(sync),
        GameModeLauncher(),
        ConflictsDiagnose(),
        ConflictResolverTool(),
    ]
