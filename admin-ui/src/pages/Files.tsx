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
} from 'antd';
import {
  FolderOutlined,
  FileOutlined,
  DeleteOutlined,
  EyeOutlined,
  LinkOutlined,
  UserOutlined,
  HomeOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { get, del } from '../services/api';

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
  const [namespaces, setNamespaces] = useState<NamespaceEntry[]>([]);
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

  const handleSelectNs = (entry: NamespaceEntry) => {
    setSelectedNs(entry);
    setCurrentPrefix('');
    fetchFolder(entry.namespace, '');
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

  // User label for sidebar
  const getUserLabel = (entry: NamespaceEntry) => {
    if (entry.displayName) return entry.displayName;
    if (entry.channelKey) return entry.channelKey;
    return entry.namespace;
  };

  return (
    <div>
      <Title level={4}>Files</Title>

      <Layout style={{ background: 'transparent', minHeight: 500 }}>
        <Sider
          width={260}
          style={{
            background: 'transparent',
            borderRight: '1px solid var(--border-color, #f0f0f0)',
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
                  <div>
                    <div style={{ lineHeight: '20px' }}>{getUserLabel(entry)}</div>
                    {entry.channelKey && entry.displayName && (
                      <Tag style={{ fontSize: 10, marginTop: 2 }}>
                        {entry.channelKey}
                      </Tag>
                    )}
                  </div>
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
