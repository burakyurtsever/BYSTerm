; BYSTerm Windows kurulum betigi (Inno Setup 6)
; CI'da derlenir:  iscc /DAppVersion=1.2.0 [/DWithCom0com=1] installer\bysterm.iss
; Cikti: installer\Output\BYSTerm-Setup-windows-x64.exe
;
; BYSTerm'i Program Files'a kurar, Baslat menusu + (istege bagli) masaustu kisayolu, kaldirici ekler.
; WithCom0com tanimliysa, seri port izleme icin ucretsiz/imzali com0com sanal null-modem surucusunu
; (kullanici onayiyla, sessizce) kurar. com0com olmadan da kurulum sorunsuz tamamlanir.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "BYSTerm"
#define AppPublisher "Burak Yurtsever"
#define AppURL "https://github.com/burakyurtsever/BYSTerm"
#define AppExe "BYSTerm.exe"

[Setup]
AppId={{B7A5E6C2-9E3F-4B1A-8C7D-BYSTERM000001}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=BYSTerm-Setup-windows-x64
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
MinVersion=6.1

[Languages]
Name: "tr"; MessagesFile: "compiler:Languages\Turkish.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
#ifdef WithCom0com
Name: "com0com"; Description: "Seri port izleme icin sanal port surucusu (com0com) kur"; GroupDescription: "Ek bilesenler:"
#endif

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; DestName: "README.txt"; Flags: ignoreversion
#ifdef WithCom0com
Source: "com0com\*"; DestDir: "{tmp}\com0com"; Flags: deleteafterinstall recursesubdirs createallsubdirs; Tasks: com0com
#endif

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\icon.ico"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
#ifdef WithCom0com
; com0com imzali kurulumu sessizce (kullanici "com0com" gorevini sectiyse). /S = sessiz.
Filename: "{tmp}\com0com\setup.exe"; Parameters: "/S"; StatusMsg: "Sanal seri port surucusu kuruluyor..."; Flags: waituntilterminated; Tasks: com0com
#endif
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; com0com'u BYSTerm kaldirilirken SILMEYIZ (baska uygulamalar kullaniyor olabilir).

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
