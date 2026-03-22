import { useState, useEffect, useCallback } from 'react';
import {
  Table,
  Tag,
  Button,
  Drawer,
  Descriptions,
  Popconfirm,
  Modal,
  Input,
  Tabs,
  Typography,
  Spin,
  Space,
  message,
} from 'antd';
import {
  UserDeleteOutlined,
  PlusOutlined,
  EyeOutlined,
  DisconnectOutlined,
  DeleteOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { get, post, del } from '../services/api';

const { Title, Text } = Typography;

interface UserChannel {
  channelKey: string;
  channel: string;
  channelUserId: string;
  boundAt?: string;
}

interface UserSummary {
  userId: string;
  displayName?: string;
  channels: UserChannel[];
  createdAt?: string;
}

interface UserDetail extends UserSummary {
  session?: {
    sessionId: string;
    createdAt: string;
    lastActivity: string;
  } | null;
  cronJobs?: {
    name: string;
    expression: string;
    message: string;
    timezone: string;
    channel: string;
  }[];
}

interface AllowlistEntry {
  channelKey: string;
  addedAt: string;
}

const CHANNEL_COLORS: Record<string, string> = {
  telegram: 'blue',
  slack: 'purple',
  feishu: 'green',
};

export default function Users() {
  const [users, setUsers] = useState<UserSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');

  const [detailUser, setDetailUser] = useState<UserDetail | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  const [allowlist, setAllowlist] = useState<AllowlistEntry[]>([]);
  const [allowlistLoading, setAllowlistLoading] = useState(false);
  const [addModalOpen, setAddModalOpen] = useState(false);
  const [newChannelKey, setNewChannelKey] = useState('');

  const fetchUsers = useCallback(() => {
    setLoading(true);
    get<{ users: UserSummary[] }>('/api/users')
      .then((data) => setUsers(data.users))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  const fetchAllowlist = useCallback(() => {
    setAllowlistLoading(true);
    get<{ entries: AllowlistEntry[] }>('/api/allowlist')
      .then((data) => setAllowlist(data.entries))
      .catch(console.error)
      .finally(() => setAllowlistLoading(false));
  }, []);

  useEffect(() => {
    fetchUsers();
    fetchAllowlist();
  }, [fetchUsers, fetchAllowlist]);

  const openDetail = async (userId: string) => {
    setDrawerOpen(true);
    setDetailLoading(true);
    try {
      const data = await get<UserDetail>(`/api/users/${userId}`);
      setDetailUser(data);
    } catch {
      message.error('Failed to load user details');
    }
    setDetailLoading(false);
  };

  const handleDeleteUser = async (userId: string) => {
    try {
      await del(`/api/users/${userId}`);
      message.success('User deleted');
      fetchUsers();
      setDrawerOpen(false);
    } catch {
      message.error('Failed to delete user');
    }
  };

  const handleUnbindChannel = async (
    userId: string,
    channelKey: string
  ) => {
    try {
      await del(
        `/api/users/${userId}/channels/${encodeURIComponent(channelKey)}`
      );
      message.success('Channel unbound');
      openDetail(userId);
      fetchUsers();
    } catch {
      message.error('Failed to unbind channel');
    }
  };

  const handleAddAllowlist = async () => {
    if (!newChannelKey || !newChannelKey.includes(':')) {
      message.warning('Channel key must be in format channel:id');
      return;
    }
    try {
      await post('/api/allowlist', { channelKey: newChannelKey });
      message.success('Added to allowlist');
      setNewChannelKey('');
      setAddModalOpen(false);
      fetchAllowlist();
    } catch {
      message.error('Failed to add to allowlist');
    }
  };

  const handleDeleteAllowlist = async (channelKey: string) => {
    try {
      await del(`/api/allowlist/${encodeURIComponent(channelKey)}`);
      message.success('Removed from allowlist');
      fetchAllowlist();
    } catch {
      message.error('Failed to remove from allowlist');
    }
  };

  const filteredUsers = search
    ? users.filter(
        (u) =>
          u.userId.toLowerCase().includes(search.toLowerCase()) ||
          (u.displayName || '')
            .toLowerCase()
            .includes(search.toLowerCase())
      )
    : users;

  const userColumns: ColumnsType<UserSummary> = [
    {
      title: 'User ID',
      dataIndex: 'userId',
      key: 'userId',
      ellipsis: true,
    },
    {
      title: 'Display Name',
      dataIndex: 'displayName',
      key: 'displayName',
      render: (v: string) => v || '-',
    },
    {
      title: 'Channels',
      dataIndex: 'channels',
      key: 'channels',
      render: (channels: UserChannel[]) =>
        channels.map((ch) => (
          <Tag
            key={ch.channelKey}
            color={CHANNEL_COLORS[ch.channel] || 'default'}
          >
            {ch.channelKey}
          </Tag>
        )),
    },
    {
      title: 'Created At',
      dataIndex: 'createdAt',
      key: 'createdAt',
      render: (v: string) => (v ? new Date(v).toLocaleString() : '-'),
      width: 180,
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 160,
      render: (_: unknown, record: UserSummary) => (
        <Space>
          <Button
            size="small"
            icon={<EyeOutlined />}
            onClick={() => openDetail(record.userId)}
          >
            Detail
          </Button>
          <Popconfirm
            title="Delete this user?"
            description="This will delete the user and all associated records."
            onConfirm={() => handleDeleteUser(record.userId)}
          >
            <Button
              size="small"
              danger
              icon={<UserDeleteOutlined />}
            />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const allowlistColumns: ColumnsType<AllowlistEntry> = [
    {
      title: 'Channel Key',
      dataIndex: 'channelKey',
      key: 'channelKey',
    },
    {
      title: 'Added At',
      dataIndex: 'addedAt',
      key: 'addedAt',
      render: (v: string) => (v ? new Date(v).toLocaleString() : '-'),
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 100,
      render: (_: unknown, record: AllowlistEntry) => (
        <Popconfirm
          title="Remove from allowlist?"
          onConfirm={() => handleDeleteAllowlist(record.channelKey)}
        >
          <Button size="small" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ];

  return (
    <div>
      <Title level={4}>Users</Title>

      <Tabs
        items={[
          {
            key: 'users',
            label: 'Users',
            children: (
              <>
                <div
                  style={{
                    marginBottom: 16,
                    display: 'flex',
                    gap: 8,
                  }}
                >
                  <Input.Search
                    placeholder="Search by user ID or display name"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    style={{ maxWidth: 400 }}
                    allowClear
                  />
                </div>
                <Table
                  columns={userColumns}
                  dataSource={filteredUsers}
                  rowKey="userId"
                  loading={loading}
                  pagination={{ pageSize: 20 }}
                  size="middle"
                />
              </>
            ),
          },
          {
            key: 'allowlist',
            label: 'Allowlist',
            children: (
              <>
                <div style={{ marginBottom: 16 }}>
                  <Button
                    type="primary"
                    icon={<PlusOutlined />}
                    onClick={() => setAddModalOpen(true)}
                  >
                    Add to Allowlist
                  </Button>
                </div>
                <Table
                  columns={allowlistColumns}
                  dataSource={allowlist}
                  rowKey="channelKey"
                  loading={allowlistLoading}
                  pagination={{ pageSize: 20 }}
                  size="middle"
                />
              </>
            ),
          },
        ]}
      />

      {/* Add to Allowlist Modal */}
      <Modal
        title="Add to Allowlist"
        open={addModalOpen}
        onOk={handleAddAllowlist}
        onCancel={() => {
          setAddModalOpen(false);
          setNewChannelKey('');
        }}
        okText="Add"
      >
        <p>
          Enter the channel key in the format{' '}
          <Text code>channel:user_id</Text> (e.g.,{' '}
          <Text code>telegram:123456789</Text>)
        </p>
        <Input
          value={newChannelKey}
          onChange={(e) => setNewChannelKey(e.target.value)}
          placeholder="telegram:123456789"
        />
      </Modal>

      {/* User Detail Drawer */}
      <Drawer
        title={`User: ${detailUser?.userId || ''}`}
        open={drawerOpen}
        onClose={() => {
          setDrawerOpen(false);
          setDetailUser(null);
        }}
        width={520}
      >
        {detailLoading ? (
          <div style={{ textAlign: 'center', padding: 40 }}>
            <Spin />
          </div>
        ) : detailUser ? (
          <>
            <Descriptions column={1} bordered size="small">
              <Descriptions.Item label="User ID">
                {detailUser.userId}
              </Descriptions.Item>
              <Descriptions.Item label="Display Name">
                {detailUser.displayName || '-'}
              </Descriptions.Item>
              <Descriptions.Item label="Created At">
                {detailUser.createdAt
                  ? new Date(detailUser.createdAt).toLocaleString()
                  : '-'}
              </Descriptions.Item>
            </Descriptions>

            <Title level={5} style={{ marginTop: 20 }}>
              Channels
            </Title>
            {detailUser.channels.length === 0 ? (
              <Text type="secondary">No channels bound</Text>
            ) : (
              detailUser.channels.map((ch) => (
                <div
                  key={ch.channelKey}
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    padding: '8px 0',
                    borderBottom: '1px solid rgba(128,128,128,0.2)',
                  }}
                >
                  <Tag color={CHANNEL_COLORS[ch.channel] || 'default'}>
                    {ch.channelKey}
                  </Tag>
                  <Popconfirm
                    title={`Unbind ${ch.channelKey}?`}
                    onConfirm={() =>
                      handleUnbindChannel(
                        detailUser.userId,
                        ch.channelKey
                      )
                    }
                  >
                    <Button
                      size="small"
                      danger
                      icon={<DisconnectOutlined />}
                    >
                      Unbind
                    </Button>
                  </Popconfirm>
                </div>
              ))
            )}

            {detailUser.session && (
              <>
                <Title level={5} style={{ marginTop: 20 }}>
                  Session
                </Title>
                <Descriptions column={1} bordered size="small">
                  <Descriptions.Item label="Session ID">
                    <Text code>{detailUser.session.sessionId}</Text>
                  </Descriptions.Item>
                  <Descriptions.Item label="Created At">
                    {detailUser.session.createdAt
                      ? new Date(
                          detailUser.session.createdAt
                        ).toLocaleString()
                      : '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="Last Activity">
                    {detailUser.session.lastActivity
                      ? new Date(
                          detailUser.session.lastActivity
                        ).toLocaleString()
                      : '-'}
                  </Descriptions.Item>
                </Descriptions>
              </>
            )}

            {detailUser.cronJobs && detailUser.cronJobs.length > 0 && (
              <>
                <Title level={5} style={{ marginTop: 20 }}>
                  Cron Jobs
                </Title>
                <Table
                  dataSource={detailUser.cronJobs}
                  rowKey="name"
                  size="small"
                  pagination={false}
                  columns={[
                    { title: 'Name', dataIndex: 'name', key: 'name' },
                    {
                      title: 'Expression',
                      dataIndex: 'expression',
                      key: 'expression',
                    },
                    {
                      title: 'Timezone',
                      dataIndex: 'timezone',
                      key: 'timezone',
                    },
                    {
                      title: 'Channel',
                      dataIndex: 'channel',
                      key: 'channel',
                    },
                  ]}
                />
              </>
            )}
          </>
        ) : null}
      </Drawer>
    </div>
  );
}
