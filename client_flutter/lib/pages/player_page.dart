import 'dart:async';
import 'package:flutter/material.dart';
import 'package:media_kit/media_kit.dart';
import 'package:media_kit_video/media_kit_video.dart';
import '../services/api_client.dart';

class NativePlayerPage extends StatefulWidget {
  final MyDriveApiClient api;
  final Map<String, dynamic> file;

  const NativePlayerPage({super.key, required this.api, required this.file});

  @override
  State<NativePlayerPage> createState() => _NativePlayerPageState();
}

class _NativePlayerPageState extends State<NativePlayerPage> {
  late final Player _player;
  late final VideoController _controller;
  double _rate = 1.0;
  String _quality = 'direct';
  List<Map<String, dynamic>> _subtitles = [];
  Timer? _progressTimer;

  @override
  void initState() {
    super.initState();
    _player = Player();
    _controller = VideoController(_player);
    _initPlayback();
  }

  Future<void> _initPlayback() async {
    final id = widget.file['identity'] as String;
    Map<String, dynamic> probe = {};
    try {
      probe = await widget.api.getMediaProbe(id);
    } catch (_) {}

    final extSubs = List<Map<String, dynamic>>.from(probe['subtitles'] ?? []);
    final embSubs = List<Map<String, dynamic>>.from(probe['embedded_subtitles'] ?? []);
    setState(() {
      _subtitles = [
        ...extSubs.map((s) => {'label': s['name'] ?? '外挂字幕', 'url': s['url']}),
        ...embSubs.map((s) => {'label': '[内封] ${s['title'] ?? '字幕'}', 'url': s['url']}),
      ];
    });

    await _player.open(Media(widget.api.streamUrl(id, quality: _quality)));

    final prog = probe['progress'];
    if (prog is Map) {
      final pos = (prog['position_sec'] ?? 0).toDouble();
      final rate = (prog['playback_rate'] ?? 1.0).toDouble();
      if (pos > 3) {
        await _player.seek(Duration(seconds: pos.floor()));
      }
      _rate = rate;
      await _player.setRate(_rate);
    }

    if (_subtitles.isNotEmpty) {
      final firstUrl = widget.api.subtitleUrl(_subtitles.first['url']);
      await _player.setSubtitleTrack(SubtitleTrack.uri(firstUrl));
    }

    _progressTimer = Timer.periodic(const Duration(seconds: 5), (_) {
      widget.api.savePlaybackProgress(
        id,
        _player.state.position.inSeconds.toDouble(),
        _player.state.duration.inSeconds.toDouble(),
        _rate,
      ).catchError((_) {});
    });
  }

  @override
  void dispose() {
    _progressTimer?.cancel();
    _player.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final id = widget.file['identity'] as String;
    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        title: Text(widget.file['name'] ?? '云播播放器'),
        actions: [
          DropdownButton<String>(
            value: _quality,
            dropdownColor: Colors.grey[900],
            style: const TextStyle(color: Colors.white),
            items: const [
              DropdownMenuItem(value: 'direct', child: Text('原画硬解')),
              DropdownMenuItem(value: '1080p', child: Text('HLS 1080P')),
              DropdownMenuItem(value: '720p', child: Text('HLS 720P')),
              DropdownMenuItem(value: '480p', child: Text('HLS 480P')),
            ],
            onChanged: (q) async {
              if (q == null) return;
              final pos = _player.state.position;
              setState(() => _quality = q);
              await _player.open(Media(widget.api.streamUrl(id, quality: q)));
              await _player.seek(pos);
            },
          ),
          DropdownButton<double>(
            value: _rate,
            dropdownColor: Colors.grey[900],
            style: const TextStyle(color: Colors.white),
            items: const [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
                .map((r) => DropdownMenuItem(value: r, child: Text('${r}x')))
                .toList(),
            onChanged: (r) async {
              if (r == null) return;
              setState(() => _rate = r);
              await _player.setRate(r);
            },
          ),
        ],
      ),
      body: Center(child: Video(controller: _controller)),
    );
  }
}
