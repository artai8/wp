import 'package:dio/dio.dart';
import 'package:shared_preferences/shared_preferences.dart';

class MyDriveApiClient {
  String baseUrl;
  String token;
  late Dio _dio;

  MyDriveApiClient({this.baseUrl = 'http://127.0.0.1:8765', this.token = ''}) {
    _rebuildDio();
  }

  void _rebuildDio() {
    _dio = Dio(BaseOptions(
      baseUrl: baseUrl.replaceAll(RegExp(r'/+$'), ''),
      connectTimeout: const Duration(seconds: 15),
      receiveTimeout: const Duration(seconds: 60),
      headers: token.isNotEmpty ? {'Authorization': 'Bearer $token'} : {},
    ));
  }

  Future<void> loadSavedSession() async {
    final prefs = await SharedPreferences.getInstance();
    baseUrl = prefs.getString('mydrive_server') ?? baseUrl;
    token = prefs.getString('mydrive_token') ?? '';
    _rebuildDio();
  }

  Future<void> saveSession(String server, String newToken) async {
    baseUrl = server.replaceAll(RegExp(r'/+$'), '');
    token = newToken;
    _rebuildDio();
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('mydrive_server', baseUrl);
    await prefs.setString('mydrive_token', token);
  }

  Future<Map<String, dynamic>> login(String server, String username, String password) async {
    baseUrl = server.replaceAll(RegExp(r'/+$'), '');
    _rebuildDio();
    final resp = await _dio.post('/api/auth/login', data: {'username': username, 'password': password});
    final data = Map<String, dynamic>.from(resp.data);
    await saveSession(baseUrl, data['access_token'] ?? '');
    return data;
  }

  Future<Map<String, dynamic>> register(String server, String username, String password) async {
    baseUrl = server.replaceAll(RegExp(r'/+$'), '');
    _rebuildDio();
    final resp = await _dio.post('/api/auth/register', data: {'username': username, 'password': password});
    final data = Map<String, dynamic>.from(resp.data);
    await saveSession(baseUrl, data['access_token'] ?? '');
    return data;
  }

  Future<List<Map<String, dynamic>>> listFiles({String parent = 'root'}) async {
    final resp = await _dio.get('/api/files', queryParameters: {'parent': parent});
    return List<Map<String, dynamic>>.from(resp.data);
  }

  Future<List<Map<String, dynamic>>> searchFiles(String q) async {
    final resp = await _dio.get('/api/search', queryParameters: {'q': q});
    return List<Map<String, dynamic>>.from(resp.data);
  }

  Future<List<Map<String, dynamic>>> listCategory(String category) async {
    final resp = await _dio.get('/api/category/$category');
    return List<Map<String, dynamic>>.from(resp.data);
  }

  Future<Map<String, dynamic>> uploadFile(String filePath, String filename, {String parent = 'root'}) async {
    final form = FormData.fromMap({
      'file': await MultipartFile.fromFile(filePath, filename: filename),
    });
    final resp = await _dio.post('/api/upload', queryParameters: {'parent': parent}, data: form);
    return Map<String, dynamic>.from(resp.data);
  }

  Future<Map<String, dynamic>> getMediaProbe(String identity) async {
    final resp = await _dio.get('/api/media/$identity/probe');
    return Map<String, dynamic>.from(resp.data);
  }

  Future<void> savePlaybackProgress(String identity, double pos, double duration, double rate) async {
    await _dio.post('/api/media/$identity/progress', data: {
      'position_sec': pos,
      'duration_sec': duration,
      'playback_rate': rate,
      'quality': 'original',
    });
  }

  String streamUrl(String identity, {String quality = 'direct'}) {
    final tok = Uri.encodeQueryComponent(token);
    if (quality == 'direct') {
      return '$baseUrl/api/download/$identity?access_token=$tok';
    }
    return '$baseUrl/api/media/$identity/hls/$quality/index.m3u8?access_token=$tok';
  }

  String subtitleUrl(String relativePath) {
    final tok = Uri.encodeQueryComponent(token);
    return '$baseUrl$relativePath?access_token=$tok';
  }

  Future<List<Map<String, dynamic>>> listOffline() async {
    final resp = await _dio.get('/api/offline/list');
    return List<Map<String, dynamic>>.from(resp.data);
  }

  Future<Map<String, dynamic>> addOffline(String url, {String savePath = '/离线下载'}) async {
    final resp = await _dio.post('/api/offline/add', data: {'url': url, 'save_path': savePath});
    return Map<String, dynamic>.from(resp.data);
  }

  Future<Map<String, dynamic>> getVipMe() async {
    final resp = await _dio.get('/api/vip/me');
    return Map<String, dynamic>.from(resp.data);
  }

  Future<Map<String, dynamic>> redeemVip(String code) async {
    final resp = await _dio.post('/api/vip/redeem', data: {'code': code});
    return Map<String, dynamic>.from(resp.data);
  }
}
