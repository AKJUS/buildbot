/*
  This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the
  MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.

  Copyright Buildbot Team Members
*/

import {createContext, useContext, useEffect, useRef} from "react";
import {DataClient} from "./DataClient";
import {IDataAccessor} from "./DataAccessor";
import {DataCollection, IDataCollection} from "./DataCollection";
import {IObservableArray} from "mobx";
import {BaseClass} from "./classes/BaseClass";
import {DataPropertiesCollection} from "./DataPropertiesCollection";

// The default value is not used as the context is injected
export const DataClientContext =
  createContext(new DataClient(undefined as any, undefined as any));

export function useDataAccessor<T>(dependency: (T|null)[]): IDataAccessor {
  const dataClient = useContext(DataClientContext);

  const storedDependency = useRef<(T|null)[]>([]);
  const accessor= useRef<IDataAccessor|null>(null);

  if (accessor.current === null) {
    accessor.current = dataClient.open();
    storedDependency.current = [...dependency];
  } else if (!arrayElementsEqual(dependency, storedDependency.current)) {
    accessor.current.close();
    accessor.current = dataClient.open();
    storedDependency.current = [...dependency];
  }

  useEffect(() => {
    if (accessor.current !== null) {
      return () => {
        accessor.current!.close();
        accessor.current = null;
      }
    }
  }, []);

  return accessor.current;
}

export function useDataApiQuery<Collection extends IDataCollection>(
    callback: () => Collection): Collection {
  let storedCollection = useRef<Collection|null>(null);
  if (storedCollection.current === null ||
      storedCollection.current.isExpired()) {
    if (storedCollection.current !== null) {
      storedCollection.current.close();
    }
    storedCollection.current = callback();
  }
  return storedCollection.current;
}

function arrayElementsEqual<T>(a: (T|null)[], b: (T|null)[]) {
  if (a.length !== b.length) {
    return false;
  }
  for (let i = 0; i < a.length; ++i) {
    if (a[i] !== b[i]) {
      return false;
    }
  }
  return true;
}

export function useDataApiDynamicQuery<T, Collection extends IDataCollection>(
    dependency: (T|null)[], callback: () => Collection): Collection {
  const storedDependency = useRef<(T|null)[]>([]);
  let storedCollection = useRef<Collection|null>(null);

  if (storedCollection.current === null ||
      !arrayElementsEqual(dependency, storedDependency.current) ||
      storedCollection.current.isExpired()) {
    if (storedCollection.current !== null) {
      storedCollection.current.close();
    }
    storedCollection.current = callback();
    storedDependency.current = [...dependency];
  }

  return storedCollection.current;
}

export function useDataApiSingleElementQuery<T extends BaseClass, U extends BaseClass>(
    el: T | null, callback: (el: T) => DataCollection<U>): DataCollection<U> {
  return useDataApiDynamicQuery([el === null],
    () => el === null ? new DataCollection<U>() : callback(el));
}

export function useDataApiSinglePropertiesQuery<T extends BaseClass>(
  el: T | null, callback: (el: T) => DataPropertiesCollection): DataPropertiesCollection {
  return useDataApiDynamicQuery([el === null],
    () => el === null ? new DataPropertiesCollection() : callback(el));
}

export function findOrNull<T>(array: IObservableArray<T>, filter: (el: T) => boolean): T | null {
  for (const el of array) {
    if (filter(el)) {
      return el;
    }
  }
  return null;
}
