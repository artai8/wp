import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:media_kit/media_kit.dart';
import 'services/api_client.dart';
import 'pages/player_page.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  MediaKit.ensureInitialized();
  runApp(const ProviderScope(child: MyDriveApp()));
}

class MyDriveApp extends StatelessWidget {
  const MyDriveApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'MyDrive 商业版客户端',
      theme: ThemeData(useMaterial3: true, colorSchemeSeed: const Color(0xFF0071E3)),
      home: const RootGate(),
    );
  }
}

class RootGate extends StatefulWidget {
  const RootGate({super.key});

  @override
  State<RootGate> createState() => _RootGateState();
}

class _RootGateState extends State<RootGate> {
  final MyDriveApiClient api = MyDriveApiClient();
  bool ready = false;

  @override
  void initState() {
    super.initState();
    api.loadSavedSession().then((_) => setState(() => ready = true));
  }

  @override
  Widget build(BuildContext context) {
    if (!ready) return const Scaffold(body: Center(child: CircularProgressIndicator()));
    if (api.token.isEmpty) {
      return LoginPage(api: api, onLoggedIn: () => setState(() {}));
    }
    return HomeShell(api: api, onLogout: () async {
      await api.saveSession(api.baseUrl, '');
      setState(() {});
    });
  }
}

class LoginPage extends StatefulWidget {
  final MyDriveApiClient api;
  final VoidCallback onLoggedIn;
  const LoginPage({super.key, required this.api, required this.onLoggedIn});

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  final _server = TextEditingController(text: 'http://127.0.0.1:8765');
  final _user = TextEditingController();
  final _pass = TextEditingController();
  String _err = '';

  Future<void> _submit(bool isReg) async {
    setState(() => _err = '');
    try {
      if (isReg) {
        await widget.api.register(_server.text.trim(), _user.text.trim(), _pass.text);
      } else {
        await widget.api.login(_server.text.trim(), _user.text.trim(), _pass.text);
      }
      widget.onLoggedIn();
    } catch (e) {
      setState(() => _err = e.toString());
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 400),
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              const Text('MyDrive 原生客户端', style: TextStyle(fontSize: 24, fontWeight: FontWeight.bold)),
              const SizedBox(height: 16),
              TextField(controller: _server, decoration: const InputDecoration(labelText: '服务端地址')),
              TextField(controller: _user, decoration: const InputDecoration(labelText: '用户名')),
              TextField(controller: _pass, obscureText: true, decoration: const InputDecoration(labelText: '密码')),
              if (_err.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 8), child: Text(_err, style: const TextStyle(color: Colors.red))),
              const SizedBox(height: 16),
              Row(children: [
                Expanded(child: FilledButton(onPressed: () => _submit(false), child: const Text('登录'))),
                const SizedBox(width: 10),
                Expanded(child: OutlinedButton(onPressed: () => _submit(true), child: const Text('注册'))),
              ]),
            ]),
          ),
        ),
      ),
    );
  }
}

class HomeShell extends StatefulWidget {
  final MyDriveApiClient api;
  final VoidCallback onLogout;
  const HomeShell({super.key, required this.api, required this.onLogout});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _tab = 0;
  List<Map<String, dynamic>> _files = [];
  List<Map<String, dynamic>> _tasks = [];
  Map<String, dynamic> _vip = {};
  final _magnetCtrl = TextEditingController();
  final _codeCtrl = TextEditingController();

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  Future<void> _refresh() async {
    try {
      if (_tab == 0) {
        _files = await widget.api.listFiles();
      } else if (_tab == 1) {
        _files = await widget.api.listCategory('video');
      } else if (_tab == 2) {
        _tasks = await widget.api.listOffline();
      } else {
        _vip = await widget.api.getVipMe();
      }
      if (mounted) setState(() {});
    } catch (_) {}
  }

  Future<void> _pickAndUpload() async {
    final res = await FilePicker.platform.pickFiles();
    if (res == null || res.files.single.path == null) return;
    await widget.api.uploadFile(res.files.single.path!, res.files.single.name);
    _refresh();
  }

  @override
  Widget build(BuildContext context) {
    Widget body;
    if (_tab == 0 || _tab == 1) {
      body = ListView.builder(
        itemCount: _files.length,
        itemBuilder: (_, i) {
          final f = _files[i];
          final isVideo = (f['mime_type'] ?? '').toString().startsWith('video/');
          return ListTile(
            leading: Icon(f['is_dir'] == true ? Icons.folder : (isVideo ? Icons.play_circle : Icons.insert_drive_file)),
            title: Text(f['name'] ?? ''),
            subtitle: Text('${((f['size'] ?? 0) / 1048576).toStringAsFixed(2)} MB'),
            onTap: isVideo
                ? () => Navigator.push(context, MaterialPageRoute(builder: (_) => NativePlayerPage(api: widget.api, file: f)))
                : null,
          );
        },
      );
    } else if (_tab == 2) {
      body = Column(children: [
        Padding(
          padding: const EdgeInsets.all(12),
          child: Row(children: [
            Expanded(child: TextField(controller: _magnetCtrl, decoration: const InputDecoration(hintText: '粘贴 magnet:? 或 HTTP 链接'))),
            const SizedBox(width: 8),
            FilledButton(
              onPressed: () async {
                if (_magnetCtrl.text.trim().isEmpty) return;
                await widget.api.addOffline(_magnetCtrl.text.trim());
                _magnetCtrl.clear();
                _refresh();
              },
              child: const Text('离线/秒存'),
            ),
          ]),
        ),
        Expanded(
          child: ListView.builder(
            itemCount: _tasks.length,
            itemBuilder: (_, i) {
              final t = _tasks[i];
              return ListTile(
                title: Text(t['name'] ?? '任务'),
                subtitle: Text('进度 ${t['progress'] ?? 0}% · ${t['save_path'] ?? '/'}'),
              );
            },
          ),
        ),
      ]);
    } else {
      body = Padding(
        padding: const EdgeInsets.all(20),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('当前等级：${_vip['plan_name'] ?? '免费版'}', style: const TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
          const SizedBox(height: 12),
          Row(children: [
            Expanded(child: TextField(controller: _codeCtrl, decoration: const InputDecoration(hintText: '输入 VIP/SVIP 兑换码'))),
            const SizedBox(width: 8),
            FilledButton(
              onPressed: () async {
                await widget.api.redeemVip(_codeCtrl.text.trim());
                _codeCtrl.clear();
                _refresh();
              },
              child: const Text('兑换'),
            ),
          ]),
        ]),
      );
    }

    return Scaffold(
      appBar: AppBar(
        title: const Text('MyDrive'),
        actions: [
          IconButton(icon: const Icon(Icons.upload_file), onPressed: _pickAndUpload),
          IconButton(icon: const Icon(Icons.refresh), onPressed: _refresh),
          IconButton(icon: const Icon(Icons.logout), onPressed: widget.onLogout),
        ],
      ),
      body: body,
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: (i) {
          setState(() => _tab = i);
          _refresh();
        },
        destinations: const [
          NavigationDestination(icon: Icon(Icons.folder), label: '文件'),
          NavigationDestination(icon: Icon(Icons.movie), label: '云播'),
          NavigationDestination(icon: Icon(Icons.cloud_download), label: '离线'),
          NavigationDestination(icon: Icon(Icons.workspace_premium), label: '会员'),
        ],
      ),
    );
  }
}
