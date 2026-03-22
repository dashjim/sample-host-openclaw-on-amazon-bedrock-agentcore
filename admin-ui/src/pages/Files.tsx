import { useState, useEffect, useCallback } from 'react';
import {
  Table,
  Button,
  Menu,
  Modal,
  Popconfirm,
  Spin,
  Typography,
  Layout,
  message,
  Breadcrumb,
  Empty,
  Tag,
  Card,
  Badge,
  Space,
  Collapse,
  Alert,
} from 'antd';
import {
  FolderOutlined,
  FileOutlined,
  DeleteOutlined,
  EyeOutlined,
  LinkOutlined,
  UserOutlined,
  HomeOutlined,
  SafetyCertificateOutlined,
  ScanOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { theme as antTheme } from 'antd';
import { get, del, post } from '../services/api';

const { Title, Text } = Typography;
const { Sider, Content } = Layout;

interface NamespaceEntry {
  namespace: string;
  userId?: string;
  displayName?: string;
  channelKey?: string;
}

interface FolderEntry {
  name: string;
  prefix: string;
}

interface FileEntry {
  name: string;
  path: string;
  size: number;
  lastModified: string;
}

interface FileContentResponse {
  content?: string;
  presignedUrl?: string;
  size: number;
}

interface ListResponse {
  folders: FolderEntry[];
  files: FileEntry[];
}

interface SkillFinding {
  code: string;
  severity: string;
  message: string;
  file: string | null;
  line: number | null;
}

interface SkillScanResult {
  name: string;
  score: number;
  grade: string;
  criticals: number;
  warnings: number;
  infos?: number;
  findings?: SkillFinding[];
  reportKey?: string;
}

interface ScanResult {
  score: number;
  grade: string;
  totalSkills: number;
  totalCriticals: number;
  skills: SkillScanResult[];
  scannedAt: string;
  scanType: string;
}

const GRADE_COLORS: Record<string, string> = {
  A: 'green', B: 'blue', C: 'gold', D: 'orange', F: 'red',
};

const SEVERITY_COLORS: Record<string, string> = {
  CRITICAL: 'red', WARNING: 'orange', INFO: 'blue',
};

const TEXT_EXTENSIONS = new Set([
  '.md', '.json', '.txt', '.js', '.ts', '.py', '.yaml', '.yml',
  '.toml', '.cfg', '.ini', '.sh', '.html', '.css', '.xml', '.csv',
]);

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

function getExtension(path: string): string {
  const dot = path.lastIndexOf('.');
  return dot >= 0 ? path.substring(dot).toLowerCase() : '';
}

type RowEntry = { type: 'folder'; data: FolderEntry } | { type: 'file'; data: FileEntry };

export default function Files() {
  const { token } = antTheme.useToken();
  const [namespaces, setNamespaces] = useState<NamespaceEntry[]>([])
  const [nsLoading, setNsLoading] = useState(true);
  const [selectedNs, setSelectedNs] = useState<NamespaceEntry | null>(null);

  const [currentPrefix, setCurrentPrefix] = useState('');
  const [folders, setFolders] = useState<FolderEntry[]>([]);
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [filesLoading, setFilesLoading] = useState(false);

  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewContent, setPreviewContent] = useState('');
  const [previewPath, setPreviewPath] = useState('');
  const [previewLoading, setPreviewLoading] = useState(false);

  const [scanResult, setScanResult] = useState<ScanResult | null>(null);
  const [scanLoading, setScanLoading] = useState(false);
  const [scanning, setScanning] = useState(false);

  useEffect(() => {
    setNsLoading(true);
    get<{ namespaces: NamespaceEntry[] }>('/api/files')
      .then((data) => setNamespaces(data.namespaces))
      .catch(console.error)
      .finally(() => setNsLoading(false));
  }, []);

  const fetchFolder = useCallback((ns: string, prefix: string) => {
    setFilesLoading(true);
    const params = prefix ? `?prefix=${encodeURIComponent(prefix)}` : '';
    get<ListResponse>(`/api/files/${ns}${params}`)
      .then((data) => {
        setFolders(data.folders || []);
        setFiles(data.files || []);
      })
      .catch(console.error)
      .finally(() => setFilesLoading(false));
  }, []);

  const fetchScanResult = useCallback((ns: string) => {
    setScanLoading(true);
    get<ScanResult & { error?: string }>(`/api/skill-eval/${ns}`)
      .then((data) => {
        if (data && data.skills && !data.error) {
          setScanResult(data);
        } else {
          setScanResult(null);
        }
      })
      .catch(() => setScanResult(null))
      .finally(() => setScanLoading(false));
  }, []);

  const handleScan = async (ns: string, action: 'audit' | 'eval' = 'audit') => {
    setScanning(true);
    try {
      await post(`/api/skill-eval/${ns}`, { action });
      message.success(action === 'audit' ? 'Scan complete' : 'Eval started');
      fetchScanResult(ns);
    } catch {
      message.error('Scan failed');
    }
    setScanning(false);
  };

  const handleSelectNs = (entry: NamespaceEntry) => {
    setSelectedNs(entry);
    setCurrentPrefix('');
    setScanResult(null);
    fetchFolder(entry.namespace, '');
    fetchScanResult(entry.namespace);
  };

  const handleNavigateFolder = (prefix: string) => {
    if (!selectedNs) return;
    setCurrentPrefix(prefix);
    fetchFolder(selectedNs.namespace, prefix);
  };

  const handlePreview = async (ns: string, filePath: string) => {
    const ext = getExtension(filePath);
    if (!TEXT_EXTENSIONS.has(ext)) {
      try {
        const data = await get<FileContentResponse>(
          `/api/files/${ns}/${filePath}`
        );
        if (data.presignedUrl) {
          window.open(data.presignedUrl, '_blank');
        }
      } catch {
        message.error('Failed to get file URL');
      }
      return;
    }

    setPreviewPath(filePath);
    setPreviewOpen(true);
    setPreviewLoading(true);
    try {
      const data = await get<FileContentResponse>(
        `/api/files/${ns}/${filePath}`
      );
      if (data.content !== undefined) {
        setPreviewContent(data.content);
      } else if (data.presignedUrl) {
        setPreviewContent('(File too large for preview. Opening in new tab...)');
        window.open(data.presignedUrl, '_blank');
      }
    } catch {
      setPreviewContent('Failed to load file content.');
    }
    setPreviewLoading(false);
  };

  const handleDelete = async (ns: string, filePath: string) => {
    try {
      await del(`/api/files/${ns}/${filePath}`);
      message.success('File deleted');
      fetchFolder(ns, currentPrefix);
    } catch {
      message.error('Failed to delete file');
    }
  };

  // Build breadcrumb from current prefix
  const breadcrumbParts = currentPrefix
    ? currentPrefix.replace(/\/$/, '').split('/')
    : [];

  const breadcrumbItems = [
    {
      title: (
        <a onClick={() => selectedNs && handleNavigateFolder('')}>
          <HomeOutlined /> {selectedNs?.displayName || selectedNs?.namespace || 'Root'}
        </a>
      ),
    },
    ...breadcrumbParts.map((part, i) => {
      const prefix = breadcrumbParts.slice(0, i + 1).join('/') + '/';
      const isLast = i === breadcrumbParts.length - 1;
      return {
        title: isLast ? (
          part
        ) : (
          <a onClick={() => handleNavigateFolder(prefix)}>{part}</a>
        ),
      };
    }),
  ];

  // Merge folders and files into one table
  const rows: RowEntry[] = [
    ...folders.map((f): RowEntry => ({ type: 'folder' as const, data: f })),
    ...files.map((f): RowEntry => ({ type: 'file' as const, data: f })),
  ];

  const columns: ColumnsType<RowEntry> = [
    {
      title: 'Name',
      key: 'name',
      render: (_: unknown, record: RowEntry) => {
        if (record.type === 'folder') {
          return (
            <a onClick={() => handleNavigateFolder(record.data.prefix)} style={{ fontWeight: 500 }}>
              <FolderOutlined style={{ marginRight: 8, color: '#faad14' }} />
              {record.data.name}
            </a>
          );
        }
        return (
          <span>
            <FileOutlined style={{ marginRight: 8, color: '#8c8c8c' }} />
            {record.data.name}
          </span>
        );
      },
    },
    {
      title: 'Size',
      key: 'size',
      width: 100,
      render: (_: unknown, record: RowEntry) =>
        record.type === 'file' ? formatBytes(record.data.size) : '-',
    },
    {
      title: 'Last Modified',
      key: 'lastModified',
      width: 180,
      render: (_: unknown, record: RowEntry) =>
        record.type === 'file' && record.data.lastModified
          ? new Date(record.data.lastModified).toLocaleString()
          : '-',
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 140,
      render: (_: unknown, record: RowEntry) => {
        if (record.type === 'folder') return null;
        const ext = getExtension(record.data.path);
        const isText = TEXT_EXTENSIONS.has(ext);
        return (
          <span style={{ display: 'flex', gap: 4 }}>
            <Button
              size="small"
              icon={isText ? <EyeOutlined /> : <LinkOutlined />}
              onClick={() =>
                selectedNs && handlePreview(selectedNs.namespace, record.data.path)
              }
            >
              {isText ? 'View' : 'Open'}
            </Button>
            <Popconfirm
              title="Delete this file?"
              onConfirm={() =>
                selectedNs && handleDelete(selectedNs.namespace, record.data.path)
              }
            >
              <Button size="small" danger icon={<DeleteOutlined />} />
            </Popconfirm>
          </span>
        );
      },
    },
  ];

  // User label for sidebar — show display name or channel, with namespace as subtitle
  const getUserLabel = (entry: NamespaceEntry) => {
    const primary = entry.displayName || entry.channelKey || entry.namespace;
    return primary;
  };

  return (
    <div>
      <Title level={4}>Files</Title>

      <Layout style={{ background: 'transparent', minHeight: 500 }}>
        <Sider
          width={260}
          style={{
            background: 'transparent',
            borderRight: `1px solid ${token.colorBorderSecondary}`,
            overflow: 'auto',
          }}
        >
          <div style={{ padding: '12px 16px', fontWeight: 500 }}>
            Users
          </div>
          {nsLoading ? (
            <div style={{ textAlign: 'center', padding: 20 }}>
              <Spin />
            </div>
          ) : namespaces.length === 0 ? (
            <Text
              type="secondary"
              style={{ display: 'block', padding: '8px 16px' }}
            >
              No user files found
            </Text>
          ) : (
            <Menu
              mode="inline"
              selectedKeys={selectedNs ? [selectedNs.namespace] : []}
              onClick={({ key }) => {
                const entry = namespaces.find((n) => n.namespace === key);
                if (entry) handleSelectNs(entry);
              }}
              items={namespaces.map((entry) => ({
                key: entry.namespace,
                icon: <UserOutlined />,
                label: (
                  <span
                    title={entry.channelKey || entry.namespace}
                    style={{ overflow: 'hidden', textOverflow: 'ellipsis', display: 'block' }}
                  >
                    {getUserLabel(entry)}
                  </span>
                ),
              }))}
            />
          )}
        </Sider>
        <Content style={{ padding: 16 }}>
          {!selectedNs ? (
            <Empty description="Select a user from the left panel to browse files" />
          ) : (
            <>
              {/* Skill Security Panel */}
              <Card
                size="small"
                style={{ marginBottom: 16 }}
                title={
                  <Space>
                    <SafetyCertificateOutlined />
                    Skill Security
                    {scanResult && (
                      <Tag color={GRADE_COLORS[scanResult.grade] || 'default'}>
                        Grade {scanResult.grade} ({scanResult.score}/100)
                      </Tag>
                    )}
                  </Space>
                }
                extra={
                  <Space>
                    {scanResult && scanResult.skills?.some((s) => s.reportKey) && (
                      <Button
                        size="small"
                        icon={<EyeOutlined />}
                        onClick={async () => {
                          const skill = scanResult.skills.find((s) => s.reportKey);
                          if (!skill?.reportKey) return;
                          try {
                            const ns = selectedNs.namespace;
                            const path = skill.reportKey.slice(ns.length + 1);
                            const data = await get<{ presignedUrl?: string; content?: string }>(
                              `/api/files/${ns}/${path}`
                            );
                            if (data.presignedUrl) {
                              window.open(data.presignedUrl, '_blank');
                            } else if (data.content) {
                              const blob = new Blob([data.content], { type: 'text/html' });
                              window.open(URL.createObjectURL(blob), '_blank');
                            }
                          } catch { message.error('Failed to open report'); }
                        }}
                      >
                        View Report
                      </Button>
                    )}
                    <Button
                      size="small"
                      icon={<ScanOutlined />}
                      loading={scanning}
                      onClick={() => handleScan(selectedNs.namespace, 'audit')}
                    >
                      Scan
                    </Button>
                  </Space>
                }
              >
                {scanLoading ? (
                  <Spin size="small" />
                ) : !scanResult ? (
                  <Text type="secondary">No scan results. Click "Scan" to run a security audit.</Text>
                ) : (
                  <>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      Last scanned: {new Date(scanResult.scannedAt).toLocaleString()} | {scanResult.totalSkills} skill(s)
                    </Text>
                    {scanResult.totalCriticals > 0 && (
                      <Alert
                        type="error"
                        message={`${scanResult.totalCriticals} critical finding(s) detected`}
                        style={{ marginTop: 8, marginBottom: 8 }}
                        showIcon
                      />
                    )}
                    {(scanResult.skills?.length ?? 0) > 0 && (
                      <Collapse
                        size="small"
                        style={{ marginTop: 8 }}
                        items={scanResult.skills.map((skill) => ({
                          key: skill.name,
                          label: (
                            <Space>
                              <Badge
                                color={GRADE_COLORS[skill.grade] || 'default'}
                                text={`${skill.name}`}
                              />
                              <Tag color={GRADE_COLORS[skill.grade]}>{skill.grade} ({skill.score})</Tag>
                              {skill.criticals > 0 && <Tag color="red">{skill.criticals} critical</Tag>}
                              {skill.warnings > 0 && <Tag color="orange">{skill.warnings} warning</Tag>}
                            </Space>
                          ),
                          children: (
                            <>
                              {skill.findings && skill.findings.length > 0 ? (
                                <Table
                                  size="small"
                                  pagination={false}
                                  dataSource={skill.findings}
                                  rowKey={(f) => `${f.code}-${f.file}-${f.line}`}
                                  columns={[
                                    {
                                      title: 'Severity',
                                      dataIndex: 'severity',
                                      width: 90,
                                      render: (s: string) => (
                                        <Tag color={SEVERITY_COLORS[s] || 'default'}>{s}</Tag>
                                      ),
                                    },
                                    { title: 'Code', dataIndex: 'code', width: 80 },
                                    { title: 'Message', dataIndex: 'message' },
                                    {
                                      title: 'File',
                                      dataIndex: 'file',
                                      width: 150,
                                      render: (f: string | null, r: SkillFinding) =>
                                        f ? `${f}${r.line ? `:${r.line}` : ''}` : '-',
                                    },
                                  ]}
                                />
                              ) : (
                                <Text type="secondary">No findings</Text>
                              )}
                            </>
                          ),
                        }))}
                      />
                    )}
                  </>
                )}
              </Card>

              <Breadcrumb items={breadcrumbItems} style={{ marginBottom: 16 }} />
              <Table
                columns={columns}
                dataSource={rows}
                rowKey={(r) =>
                  r.type === 'folder' ? `d:${r.data.prefix}` : `f:${r.data.path}`
                }
                loading={filesLoading}
                pagination={{ pageSize: 50 }}
                size="middle"
                locale={{ emptyText: <Empty description="Empty folder" /> }}
              />
            </>
          )}
        </Content>
      </Layout>

      {/* File Preview Modal */}
      <Modal
        title={previewPath}
        open={previewOpen}
        onCancel={() => {
          setPreviewOpen(false);
          setPreviewContent('');
          setPreviewPath('');
        }}
        footer={null}
        width={700}
      >
        {previewLoading ? (
          <div style={{ textAlign: 'center', padding: 40 }}>
            <Spin />
          </div>
        ) : (
          <pre
            style={{
              maxHeight: 500,
              overflow: 'auto',
              padding: 16,
              borderRadius: 4,
              fontSize: 12,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            {previewContent}
          </pre>
        )}
      </Modal>
    </div>
  );
}
