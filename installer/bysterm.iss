; BYSTerm Windows kurulum betigi (Inno Setup 6)
; CI'da derlenir:  iscc /DAppVersion=0.3.1 [/DWithCom0com=1] [/DWithUSBPcap=1] installer\bysterm.iss
; Cikti: installer\Output\BYSTerm-Setup-windows-x64.exe
;
; BYSTerm'i Program Files'a kurar, Baslat menusu + (istege bagli) masaustu kisayolu, kaldirici ekler.
; WithCom0com tanimliysa, seri port izleme icin ucretsiz/imzali com0com sanal null-modem surucusunu
; (kullanici onayiyla, sessizce) kurar. com0com olmadan da kurulum sorunsuz tamamlanir.
; WithUSBPcap tanimliysa, Seri Izleme (sanal port olmadan USB-seri trafigini dinleme) icin
; USBPcap'in resmi, Microsoft imzali kurulumunu (GPLv2, kaynak: github.com/desowin/usbpcap) calistirir.

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

[CustomMessages]
tr.Extras=Ek bileşenler:
en.Extras=Additional components:
tr.TaskUSBPcap=Seri port dinleme (Seri İzleme) için USBPcap sürücüsünü kur — yeniden başlatma gerekir
en.TaskUSBPcap=Install the USBPcap driver for serial port capture (Serial Monitor) — restart required
tr.TaskCom0com=Sanal port için com0com sürücüsünü kur
en.TaskCom0com=Install the com0com driver for virtual ports
tr.InstUSBPcap=USB dinleme sürücüsü (USBPcap) kuruluyor...
en.InstUSBPcap=Installing USB capture driver (USBPcap)...
tr.InstCom0com=Sanal seri port sürücüsü kuruluyor...
en.InstCom0com=Installing virtual serial port driver...

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
#ifdef WithUSBPcap
Name: "usbpcap"; Description: "{cm:TaskUSBPcap}"; GroupDescription: "{cm:Extras}"; Check: not USBPcapInstalled
#endif
#ifdef WithCom0com
Name: "com0com"; Description: "{cm:TaskCom0com}"; GroupDescription: "{cm:Extras}"; Flags: unchecked
#endif

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; DestName: "README.txt"; Flags: ignoreversion
#ifdef WithUSBPcap
Source: "USBPcapSetup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Tasks: usbpcap
#endif
#ifdef WithCom0com
Source: "com0com\*"; DestDir: "{tmp}\com0com"; Flags: deleteafterinstall recursesubdirs createallsubdirs; Tasks: com0com
#endif

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\icon.ico"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
#ifdef WithUSBPcap
; USBPcap resmi imzali kurulumu, sessiz (/S). Surucu yeniden baslatmadan sonra etkin olur.
Filename: "{tmp}\USBPcapSetup.exe"; Parameters: "/S"; StatusMsg: "{cm:InstUSBPcap}"; Flags: waituntilterminated; Tasks: usbpcap
#endif
#ifdef WithCom0com
; com0com imzali kurulumu sessizce (kullanici "com0com" gorevini sectiyse). /S = sessiz.
Filename: "{tmp}\com0com\setup.exe"; Parameters: "/S"; StatusMsg: "{cm:InstCom0com}"; Flags: waituntilterminated; Tasks: com0com
#endif
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; com0com / USBPcap'i BYSTerm kaldirilirken SILMEYIZ (baska uygulamalar da kullaniyor olabilir).

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

function USBPcapInstalled(): Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf64}\USBPcap\USBPcapCMD.exe')) or
            FileExists(ExpandConstant('{commonpf32}\USBPcap\USBPcapCMD.exe'));
end;

function NeedRestart(): Boolean;
begin
#ifdef WithUSBPcap
  Result := WizardIsTaskSelected('usbpcap');
#else
  Result := False;
#endif
end;
