; Dilmaç installer (Inno Setup 6). Built by `py pc\build_exe.py --setup`, which passes AppVersion, Project and
; (if present) VoiceModel. Per user, no admin rights: the program goes to %LOCALAPPDATA%\Programs\Dilmac, its data
; (settings, packs, logs) to %LOCALAPPDATA%\Dilmac (paths.py), so a reinstall or update keeps the settings.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Project
  #define Project ".."
#endif

[Setup]
AppId={{6C1F3B9E-2D4A-4E8B-9F17-5A2C0D7E4B31}
AppName=Dilmaç
AppVersion={#AppVersion}
AppVerName=Dilmaç {#AppVersion}
AppPublisher=Dilmaç
AppComments=PS5 oyunlarının İngilizce altyazısını Türkçeye çevirip TV'de gösterir.
DefaultDirName={localappdata}\Programs\Dilmac
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#Project}
OutputBaseFilename=Dilmac-Setup-{#AppVersion}
SetupIconFile={#Project}\pc\icon.ico
UninstallDisplayIcon={app}\Dilmac.exe
UninstallDisplayName=Dilmaç
LicenseFile={#Project}\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "tr"; MessagesFile: "compiler:Languages\Turkish.isl"

[Tasks]
Name: "desktopicon"; Description: "Masaüstüne kısayol koy"; GroupDescription: "Kısayollar:"

[Files]
Source: "{#Project}\Dilmac.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Project}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
#ifdef VoiceModel
; Speaker recognition by voice (audio.py looks for it in the data folder). Kept on uninstall like the other data.
Source: "{#VoiceModel}"; DestDir: "{localappdata}\Dilmac\voices\model"; Flags: onlyifdoesntexist uninsneveruninstall
#endif

[Icons]
Name: "{autoprograms}\Dilmaç"; Filename: "{app}\Dilmac.exe"
Name: "{autodesktop}\Dilmaç"; Filename: "{app}\Dilmac.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Dilmac.exe"; Description: "Dilmaç'ı şimdi aç"; Flags: nowait postinstall skipifsilent
