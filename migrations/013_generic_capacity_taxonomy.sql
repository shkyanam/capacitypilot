alter table capacity_planner.local_capacity_reservation
  drop constraint if exists local_capacity_reservation_service_check,
  drop constraint if exists local_capacity_reservation_vault_type_check,
  drop constraint if exists local_capacity_reservation_tenancy_type_check,
  drop constraint if exists ck_local_reservation_service_vault;

update capacity_planner.local_capacity_reservation
set vault_type = case
      when service='ExaDB-XS' and vault_type='DB Vault' then 'High Performance'
      when service='ExaDB-XS' and vault_type='Flash Cache Vault' then 'Ultra Performance'
      when service='ExaDB-XS' and vault_type='System Vault' then 'System Critical'
      when service='BaseDB' and vault_type='DB Vault' then 'Standard'
      when service='BaseDB' and vault_type='System Vault' then 'System Standard'
      when service='GoldenGate' and vault_type='System Vault' then 'Replication'
      when service='GoldenGate' and vault_type='General Vault' then 'General Purpose'
      else vault_type
    end,
    tenancy_type = case
      when service in ('ExaDB-XS','BaseDB') and tenancy_type='DBAAS' then 'Shared'
      when service in ('ExaDB-XS','BaseDB') and tenancy_type='Exacompute' then 'Dedicated'
      when service='GoldenGate' then 'Replicated'
      else tenancy_type
    end,
    service = 'Storage Capacity';

alter table capacity_planner.local_capacity_reservation
  add constraint local_capacity_reservation_service_check
    check (service='Storage Capacity'),
  add constraint local_capacity_reservation_vault_type_check
    check (vault_type in ('Standard','High Performance','Ultra Performance',
      'System Standard','System Critical','Replication','General Purpose')),
  add constraint local_capacity_reservation_tenancy_type_check
    check (tenancy_type in ('Shared','Dedicated','Replicated')),
  add constraint ck_local_reservation_service_vault
    check (service='Storage Capacity');

alter table capacity_planner.capacity_inventory
  drop constraint if exists capacity_inventory_service_check,
  drop constraint if exists capacity_inventory_vault_type_check,
  drop constraint if exists capacity_inventory_tenancy_type_check,
  drop constraint if exists ck_capacity_inventory_service_vault;

update capacity_planner.capacity_inventory
set vault_type = case
      when service='ExaDB-XS' and vault_type='DB Vault' then 'High Performance'
      when service='ExaDB-XS' and vault_type='Flash Cache Vault' then 'Ultra Performance'
      when service='ExaDB-XS' and vault_type='System Vault' then 'System Critical'
      when service='BaseDB' and vault_type='DB Vault' then 'Standard'
      when service='BaseDB' and vault_type='System Vault' then 'System Standard'
      when service='GoldenGate' and vault_type='System Vault' then 'Replication'
      when service='GoldenGate' and vault_type='General Vault' then 'General Purpose'
      else vault_type
    end,
    tenancy_type = case
      when service in ('ExaDB-XS','BaseDB') and tenancy_type='DBAAS' then 'Shared'
      when service in ('ExaDB-XS','BaseDB') and tenancy_type='Exacompute' then 'Dedicated'
      when service='GoldenGate' then 'Replicated'
      else tenancy_type
    end,
    service = 'Storage Capacity';

alter table capacity_planner.capacity_inventory
  add constraint capacity_inventory_service_check
    check (service='Storage Capacity'),
  add constraint capacity_inventory_vault_type_check
    check (vault_type in ('Standard','High Performance','Ultra Performance',
      'System Standard','System Critical','Replication','General Purpose')),
  add constraint capacity_inventory_tenancy_type_check
    check (tenancy_type in ('Shared','Dedicated','Replicated')),
  add constraint ck_capacity_inventory_service_vault
    check (service='Storage Capacity');
