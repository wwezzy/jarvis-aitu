; Per-user installation only. Application data is deliberately retained on
; uninstall; no user data, env secrets or network credentials are bundled.
#ifndef BundleDir
  #error BundleDir must point to the verified PyInstaller output.
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\desktop"
#endif

[Setup]
AppId={{8C4D752A-F032-4B93-9A13-470BDE75B420}
AppName=Jarvis Desktop
AppVersion=4.2.0
AppPublisher=Jarvis personal project
DefaultDirName={localappdata}\Programs\JarvisDesktop
DefaultGroupName=Jarvis Desktop
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=JarvisDesktop-Setup-4.2.0
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\JarvisDesktop.exe
SetupIconFile=assets\jarvis.ico
CloseApplications=yes
RestartApplications=no
InfoBeforeFile=NOTICE.txt

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Jarvis Desktop"; Filename: "{app}\JarvisDesktop.exe"
Name: "{autodesktop}\Jarvis Desktop"; Filename: "{app}\JarvisDesktop.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\JarvisDesktop.exe"; Description: "Open Jarvis Desktop"; Flags: nowait postinstall skipifsilent unchecked
